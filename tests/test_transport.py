"""HttpTransport against a real local http.server: gzip, batching, retries, 429, drops."""

from __future__ import annotations

import gzip
import json
import time
import uuid
from typing import Any, List

import pytest

from faultline.domain import Event
from faultline.infrastructure import HttpTransport, StderrLogger
from tests.conftest import IngestServer

from .contract import BatchModel


def event(message: str = "hello", **extra: Any) -> Event:
    ev: Event = {
        "eventId": str(uuid.uuid4()),
        "timestamp": "2026-10-09T12:00:00.000Z",
        "level": "error",
        "platform": "python",
        "sdk": {"name": "getfaultline", "version": "0.1.0"},
        "message": message,
    }
    ev.update(extra)  # type: ignore[typeddict-item]
    return ev


def transport(url: str, **kw: Any) -> HttpTransport:
    kw.setdefault("backoff_base", 0.01)
    kw.setdefault("rand", lambda: 0.5)
    return HttpTransport(url, "flt_ingest_test", StderrLogger(False), **kw)


def test_posts_gzip_json_batch_with_headers(ingest: IngestServer) -> None:
    t = transport(ingest.url)
    t.send(event("a"))
    t.send(event("b"))
    assert t.flush(5)
    req = ingest.received[0]
    assert req.path == "/api/v1/ingest/events"
    assert req.headers["x-faultline-key"] == "flt_ingest_test"
    assert req.headers["content-type"] == "application/json"
    assert req.headers["content-encoding"] == "gzip"
    assert json.loads(gzip.decompress(req.raw)) == req.body
    BatchModel.model_validate(req.body)
    assert [e["message"] for e in req.body["events"]] == ["a", "b"]
    t.close(1)


def test_without_compression(ingest: IngestServer) -> None:
    t = transport(ingest.url, compress=False)
    t.send(event())
    assert t.flush(5)
    assert "content-encoding" not in ingest.received[0].headers
    t.close(1)


def test_sends_when_flush_at_is_reached_without_waiting_for_the_interval(
    ingest: IngestServer,
) -> None:
    t = transport(ingest.url, flush_interval=60, flush_at=20)
    for i in range(20):
        t.send(event(str(i)))
    ingest.wait_for(1, timeout=3)
    assert len(ingest.received[0].body["events"]) == 20
    t.close(1)


def test_sends_on_the_interval(ingest: IngestServer) -> None:
    t = transport(ingest.url, flush_interval=0.1)
    t.send(event())
    ingest.wait_for(1, timeout=3)
    t.close(1)


def test_batches_hold_at_most_50_events(ingest: IngestServer) -> None:
    t = transport(ingest.url, flush_interval=60, flush_at=1000, max_queue_size=120)
    for i in range(120):
        t.send(event(str(i)))
    assert t.flush(5)
    sizes = [len(r.body["events"]) for r in ingest.received]
    assert sizes == [50, 50, 20]
    t.close(1)


def test_batches_respect_the_1mb_body_limit(ingest: IngestServer) -> None:
    t = transport(ingest.url, flush_interval=60, flush_at=1000)
    big = "x" * 8000
    for i in range(30):
        t.send(event(str(i), extra={f"k{j}": big for j in range(5)}))
    assert t.flush(5)
    assert all(len(r.raw) > 0 for r in ingest.received)
    assert sum(len(r.body["events"]) for r in ingest.received) == 30
    for r in ingest.received:
        assert len(json.dumps(r.body).encode()) <= 1024 * 1024
    t.close(1)


def test_oversized_event_is_trimmed_then_sent(ingest: IngestServer) -> None:
    t = transport(ingest.url)
    huge = {f"k{i}": "y" * 8000 for i in range(200)}  # about 1.6 MB of extra
    crumbs = [{"timestamp": "t", "type": "default", "message": "m"}]
    t.send(event("big", extra=huge, breadcrumbs=crumbs))
    assert t.flush(5)
    sent = ingest.events[0]
    assert sent["message"] == "big"
    assert "extra" not in sent and "breadcrumbs" not in sent
    t.close(1)


def test_queue_drops_oldest_when_full(ingest: IngestServer) -> None:
    t = transport(ingest.url, flush_interval=60, flush_at=1000, max_queue_size=100)
    t._ensure_worker = lambda: True  # type: ignore[method-assign]  # keep events queued
    for i in range(130):
        t.send(event(str(i)))
    assert t.queue_length == 100
    del t._ensure_worker
    assert t.flush(5)
    messages = [e["message"] for e in ingest.events]
    assert messages[0] == "30" and messages[-1] == "129" and len(messages) == 100
    t.close(1)


def test_retries_5xx_with_backoff(ingest: IngestServer) -> None:
    ingest.respond(500)
    ingest.respond(503)
    t = transport(ingest.url)
    t.send(event())
    assert t.flush(5)
    assert len(ingest.received) == 3
    assert len(ingest.events) == 3  # same batch posted three times
    t.close(1)


def test_gives_up_after_max_retries(ingest: IngestServer) -> None:
    for _ in range(10):
        ingest.respond(500)
    t = transport(ingest.url, max_retries=2)
    t.send(event())
    assert t.flush(5)
    assert len(ingest.received) == 3
    t.close(1)


@pytest.mark.parametrize("status", [401, 413, 400])
def test_drops_without_retry_on_client_errors(ingest: IngestServer, status: int) -> None:
    ingest.respond(status)
    t = transport(ingest.url)
    t.send(event())
    assert t.flush(5)
    time.sleep(0.1)
    assert len(ingest.received) == 1
    assert t.queue_length == 0
    t.close(1)


def test_honors_429_retry_after(ingest: IngestServer) -> None:
    ingest.respond(429, {"Retry-After": "1"})
    t = transport(ingest.url)
    t.send(event("limited"))
    assert t.flush(5)
    assert len(ingest.received) == 2
    gap = ingest.received[1].at - ingest.received[0].at
    assert gap >= 0.9
    assert ingest.received[1].body["events"][0]["message"] == "limited"
    t.close(1)


def test_flush_returns_false_when_rate_limit_outlasts_timeout(ingest: IngestServer) -> None:
    ingest.respond(429, {"Retry-After": "30"})
    t = transport(ingest.url)
    t.send(event())
    start = time.monotonic()
    assert t.flush(0.5) is False
    assert time.monotonic() - start < 1.5
    t.close(0.2)


def test_retry_after_parsing() -> None:
    t = transport("http://127.0.0.1:9", max_retry_after=600)
    assert t._retry_after("5") == 5
    assert t._retry_after(None) == 10
    assert t._retry_after("garbage") == 10
    assert t._retry_after("99999") == 600
    from email.utils import formatdate

    assert 25 <= t._retry_after(formatdate(time.time() + 30, usegmt=True)) <= 31


def test_backoff_uses_equal_jitter() -> None:
    lo = HttpTransport("http://x", "k", StderrLogger(False), rand=lambda: 0.0)
    hi = HttpTransport("http://x", "k", StderrLogger(False), rand=lambda: 0.999999)
    assert lo.backoff(0) == 0.5 and round(hi.backoff(0), 3) == 1.0
    assert lo.backoff(3) == 4.0 and round(hi.backoff(3), 3) == 8.0
    assert lo.backoff(10) == 15.0  # capped at 30 s


def test_timeout_is_retried(ingest: IngestServer) -> None:
    ingest.delay = 0.6
    t = transport(ingest.url, timeout=0.2, max_retries=1)
    t.send(event())
    t.flush(3)
    assert t.queue_length == 0
    t.close(0.5)


def test_close_never_hangs_on_an_unresponsive_server(ingest: IngestServer) -> None:
    ingest.delay = 5
    t = transport(ingest.url)
    t.send(event())
    start = time.monotonic()
    assert t.close(0.5) is False
    assert time.monotonic() - start < 1.5
    t.send(event())  # ignored after close
    assert t.queue_length == 0


def test_send_and_flush_never_raise_on_bad_input() -> None:
    t = transport("http://127.0.0.1:9")
    t.send({"not": "an event"})  # type: ignore[arg-type]
    t.send(None)  # type: ignore[arg-type]
    assert t.close(0.1) in (True, False)


def test_unreachable_server_eventually_drops(ingest: IngestServer) -> None:
    t = transport("http://127.0.0.1:9", max_retries=1)
    t.send(event())
    t.flush(2)
    assert t.queue_length == 0
    t.close(0.2)


def test_events_conform_to_the_batch_contract(ingest: IngestServer) -> None:
    import faultline

    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url)
    try:
        {}["missing"]
    except KeyError:
        faultline.capture_exception(tags={"t": "1"}, extra={"card": "4111 1111 1111 1111"})
    faultline.capture_message("warn", "warning")
    assert faultline.flush(5)
    batches: List[Any] = [BatchModel.model_validate(r.body) for r in ingest.received]
    assert sum(len(b.events) for b in batches) == 2
    assert ingest.events[0]["extra"]["card"] == "[Filtered]"
