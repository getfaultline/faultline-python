"""Batched HTTP transport on a background worker thread.

In-memory queue (max 100, drop oldest), a batch is sent every 2 s or as soon as 20 events are
queued, batches hold at most 50 events / 1 MB, bodies are gzip-compressed, each request times
out after 5 s, failures are retried with exponential backoff plus jitter, ``429 Retry-After``
is honored, and ``401`` / ``413`` drop the batch. The worker is a daemon thread, so it can never
keep the interpreter alive. No method ever raises.
"""

from __future__ import annotations

import email.utils
import gzip
import json
import os
import random
import threading
import time
import urllib.error
import urllib.request
import weakref
from collections import deque
from dataclasses import dataclass, replace
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from ..application.ports import TRANSPORT_THREAD_NAME, Logger
from ..domain import INGEST_PATH, Event, serialize_within_limit
from ..domain.limits import MAX_BATCH_EVENTS, MAX_BODY_BYTES

WORKER_THREAD_NAME = TRANSPORT_THREAD_NAME
_ENVELOPE_BYTES = len(b'{"events":[]}')


@dataclass(frozen=True)
class _Item:
    event_id: str
    data: bytes
    rate_limited_attempts: int = 0


@dataclass(frozen=True)
class _Result:
    kind: str  # "ok" | "drop" | "rate_limited" | "retry"
    status: int = 0
    retry_after: float = 0.0
    reason: str = ""
    body: Any = None


#: (url, body, headers, timeout) -> (status, headers-getter, body bytes). Injectable for tests.
Poster = Callable[
    [str, bytes, Dict[str, str], float], Tuple[int, Callable[[str], Optional[str]], bytes]
]


def urllib_poster() -> Poster:
    opener = urllib.request.build_opener()

    def post(
        url: str, body: bytes, headers: Dict[str, str], timeout: float
    ) -> Tuple[int, Callable[[str], Optional[str]], bytes]:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with opener.open(req, timeout=timeout) as res:
                status = int(res.status)
                data = res.read(64 * 1024)
                return status, res.headers.get, data
        except urllib.error.HTTPError as err:
            hdrs = err.headers
            try:
                err.read(64 * 1024)
                err.close()
            except Exception:
                pass
            return int(err.code), (hdrs.get if hdrs is not None else (lambda _k: None)), b""

    return post


_TRANSPORTS: weakref.WeakSet[HttpTransport] = weakref.WeakSet()


def _after_fork_in_child() -> None:
    for transport in list(_TRANSPORTS):
        transport._reset_after_fork()


if hasattr(os, "register_at_fork"):
    os.register_at_fork(after_in_child=_after_fork_in_child)


class HttpTransport:
    def __init__(
        self,
        endpoint: str,
        api_key: str,
        logger: Logger,
        *,
        flush_interval: float = 2.0,
        flush_at: int = 20,
        max_queue_size: int = 100,
        max_batch_size: int = MAX_BATCH_EVENTS,
        max_body_bytes: int = MAX_BODY_BYTES,
        timeout: float = 5.0,
        max_retries: int = 3,
        backoff_base: float = 1.0,
        backoff_max: float = 30.0,
        max_retry_after: float = 600.0,
        compress: bool = True,
        poster: Optional[Poster] = None,
        rand: Callable[[], float] = random.random,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = endpoint.rstrip("/") + INGEST_PATH
        self._api_key = api_key
        self._log = logger
        self.flush_interval = flush_interval
        self.flush_at = max(1, flush_at)
        self.max_queue_size = max(1, max_queue_size)
        self.max_batch_size = max(1, min(max_batch_size, MAX_BATCH_EVENTS))
        self.max_body_bytes = min(max_body_bytes, MAX_BODY_BYTES)
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.max_retry_after = max_retry_after
        self.compress = compress
        self._post = poster or urllib_poster()
        self._rand = rand
        self._clock = clock
        self._init_state()
        _TRANSPORTS.add(self)

    def _init_state(self) -> None:
        self._queue: Deque[_Item] = deque()
        self._cond = threading.Condition(threading.Lock())
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._worker: Optional[threading.Thread] = None
        self._sending = False
        self._flush_requested = False
        self._rate_limited_until = 0.0
        self._closed = False

    def _reset_after_fork(self) -> None:
        """The worker thread does not survive ``fork()``; start fresh in the child."""
        closed, pending = self._closed, list(self._queue)
        self._init_state()
        self._closed = closed
        self._queue.extend(pending)

    # -- public -----------------------------------------------------------------------------

    @property
    def queue_length(self) -> int:
        return len(self._queue)

    def send(self, event: Event) -> None:
        try:
            if self._closed:
                return
            serialized = serialize_within_limit(event, self.max_body_bytes - _ENVELOPE_BYTES)
            if serialized is None:
                self._log.warn(f"event {event['eventId']} exceeds the 1 MB body limit; dropped")
                return
            with self._cond:
                self._queue.append(_Item(event["eventId"], serialized[1]))
                while len(self._queue) > self.max_queue_size:
                    dropped = self._queue.popleft()
                    self._log.debug(f"queue full; dropped oldest event {dropped.event_id}")
                if len(self._queue) >= self.flush_at:
                    self._cond.notify_all()
            self._ensure_worker()
        except Exception as err:
            self._log.error("transport.send failed", err)

    def flush(self, timeout: float = 2.0) -> bool:
        try:
            deadline = self._clock() + max(0.0, timeout)
            if not self._ensure_worker():
                return self._drain_inline(deadline)
            with self._cond:
                self._flush_requested = True
                self._cond.notify_all()
                while self._queue or self._sending:
                    remaining = deadline - self._clock()
                    if remaining <= 0:
                        return False
                    if not self._sending and self._rate_limited_until - self._clock() >= remaining:
                        return False
                    if self._worker is None or not self._worker.is_alive():
                        break
                    self._cond.wait(min(remaining, 0.1))
                else:
                    return True
            return self._drain_inline(deadline)
        except Exception as err:
            self._log.error("transport.flush failed", err)
            return False

    def close(self, timeout: float = 2.0) -> bool:
        try:
            start = self._clock()
            ok = self.flush(timeout)
            with self._cond:
                self._closed = True
                dropped = len(self._queue)
                self._queue.clear()
                self._cond.notify_all()
            self._stop.set()
            if dropped:
                self._log.debug(f"closed with {dropped} unsent event(s)")
            worker = self._worker
            if worker is not None and worker is not threading.current_thread():
                worker.join(max(0.0, min(0.5, timeout - (self._clock() - start))))
            return ok
        except Exception as err:
            self._log.error("transport.close failed", err)
            return False

    def backoff(self, attempt: int) -> float:
        """Exponential backoff with "equal jitter": half fixed, half random."""
        exp = min(self.backoff_max, self.backoff_base * (2.0**attempt))
        return exp / 2 + self._rand() * (exp / 2)

    # -- worker -----------------------------------------------------------------------------

    def _ensure_worker(self) -> bool:
        worker = self._worker
        if worker is not None and worker.is_alive():
            return True
        if self._closed:
            return False
        try:
            t = threading.Thread(target=self._run, name=WORKER_THREAD_NAME, daemon=True)
            t.start()
            self._worker = t
            return True
        except Exception as err:
            # e.g. "can't create new thread at interpreter shutdown"
            self._log.debug("could not start the transport worker", err)
            return False

    def _run(self) -> None:
        try:
            while not self._stop.is_set():
                with self._cond:
                    deadline = self._clock() + self.flush_interval
                    while not self._closed and not self._flush_requested:
                        if len(self._queue) >= self.flush_at and not self._rate_limited():
                            break
                        remaining = deadline - self._clock()
                        if remaining <= 0:
                            break
                        self._cond.wait(remaining)
                    self._flush_requested = False
                    if self._closed:
                        return
                self._drain(None)
                wait = self._rate_limited_until - self._clock()
                if wait > 0 and self._queue:
                    if self._stop.wait(wait):
                        return
                    with self._cond:
                        self._flush_requested = True
        except Exception as err:
            self._log.error("transport worker crashed", err)

    def _rate_limited(self) -> bool:
        return self._rate_limited_until > self._clock()

    def _drain_inline(self, deadline: float) -> bool:
        """Send from the calling thread (worker unavailable, e.g. at interpreter shutdown)."""
        self._drain(deadline)
        return not self._queue and not self._sending

    def _drain(self, deadline: Optional[float]) -> None:
        if not self._send_lock.acquire(timeout=0 if deadline is None else 0.05):
            return
        try:
            while True:
                if self._stop.is_set() or self._rate_limited():
                    return
                if deadline is not None and self._clock() >= deadline:
                    return
                with self._cond:
                    batch = self._take_batch()
                    if not batch:
                        return
                    self._sending = True
                try:
                    self._send_batch(batch, deadline)
                finally:
                    with self._cond:
                        self._sending = False
                        self._cond.notify_all()
        finally:
            self._send_lock.release()

    def _take_batch(self) -> List[_Item]:
        batch: List[_Item] = []
        size = _ENVELOPE_BYTES
        while self._queue and len(batch) < self.max_batch_size:
            nxt = self._queue[0]
            add = len(nxt.data) + (1 if batch else 0)
            if size + add > self.max_body_bytes:
                break
            size += add
            batch.append(self._queue.popleft())
        return batch

    def _send_batch(self, batch: List[_Item], deadline: Optional[float]) -> None:
        body = b'{"events":[' + b",".join(i.data for i in batch) + b"]}"
        attempt = 0
        while True:
            result = self._post_once(body, deadline)
            if result.kind == "ok":
                rejected = result.body.get("rejected") if isinstance(result.body, dict) else None
                if isinstance(rejected, list) and rejected:
                    self._log.debug(f"server rejected {len(rejected)} event(s)", rejected)
                return
            if result.kind == "drop":
                why = {401: " (invalid or revoked API key)", 413: " (payload too large)"}
                self._log.debug(
                    f"ingest responded {result.status}{why.get(result.status, '')};"
                    f" dropped {len(batch)} event(s)"
                )
                return
            if result.kind == "rate_limited":
                keep = [
                    replace(i, rate_limited_attempts=i.rate_limited_attempts + 1)
                    for i in batch
                    if i.rate_limited_attempts + 1 <= self.max_retries
                ]
                if len(keep) < len(batch):
                    self._log.debug(
                        f"dropped {len(batch) - len(keep)} event(s) after repeated 429s"
                    )
                with self._cond:
                    self._rate_limited_until = self._clock() + result.retry_after
                    self._queue.extendleft(reversed(keep))
                    while len(self._queue) > self.max_queue_size:
                        self._queue.popleft()
                self._log.debug(f"rate limited; retrying after {result.retry_after:.1f}s")
                return
            # retry
            if attempt >= self.max_retries:
                self._log.debug(
                    f"giving up after {attempt + 1} attempts ({result.reason});"
                    f" dropped {len(batch)} event(s)"
                )
                return
            delay = self.backoff(attempt)
            if deadline is not None and self._clock() + delay >= deadline:
                self._log.debug(f"send failed ({result.reason}); no time left to retry")
                return
            self._log.debug(f"send failed ({result.reason}); retry #{attempt + 1} in {delay:.2f}s")
            if self._stop.wait(delay):
                return
            attempt += 1

    def _post_once(self, body: bytes, deadline: Optional[float]) -> _Result:
        headers = {
            "Content-Type": "application/json",
            "X-Faultline-Key": self._api_key,
            "User-Agent": "getfaultline-python",
        }
        payload = body
        if self.compress:
            payload = gzip.compress(body, compresslevel=6)
            headers["Content-Encoding"] = "gzip"
        timeout = self.timeout
        if deadline is not None:
            timeout = max(0.05, min(timeout, deadline - self._clock()))
        try:
            status, get_header, data = self._post(self.url, payload, headers, timeout)
        except Exception as err:
            reason = (
                "timeout" if isinstance(err, TimeoutError) else (str(err) or type(err).__name__)
            )
            return _Result("retry", reason=reason)
        if status == 429:
            return _Result(
                "rate_limited",
                status=status,
                retry_after=self._retry_after(get_header("Retry-After")),
            )
        if 200 <= status < 300:
            parsed: Any = None
            try:
                parsed = json.loads(data.decode("utf-8")) if data else None
            except Exception:
                parsed = None
            return _Result("ok", status=status, body=parsed)
        if status >= 500 or status == 408:
            return _Result("retry", status=status, reason=f"HTTP {status}")
        return _Result("drop", status=status)

    def _retry_after(self, header: Optional[str]) -> float:
        fallback = 10.0
        if not header:
            return fallback
        seconds: float
        try:
            seconds = float(header.strip())
        except ValueError:
            try:
                when = email.utils.parsedate_to_datetime(header)
                seconds = when.timestamp() - time.time()
            except Exception:
                seconds = fallback
        if seconds != seconds:  # NaN
            seconds = fallback
        return min(max(0.0, seconds), self.max_retry_after)
