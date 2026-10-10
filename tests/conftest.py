from __future__ import annotations

import gzip
import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, List, Optional, Tuple

import pytest

import faultline
from faultline.application import ClientDeps, FaultlineClient, Hub
from faultline.domain import Event, ResolvedOptions
from faultline.infrastructure import (
    LinecacheSourceReader,
    PythonRuntimeInfo,
    StderrLogger,
    TracebackStackExtractor,
)


class FakeTransport:
    def __init__(self) -> None:
        self.events: List[Event] = []
        self.flushed = 0
        self.closed = False

    def send(self, event: Event) -> None:
        self.events.append(event)

    def flush(self, timeout: float = 2.0) -> bool:
        self.flushed += 1
        return True

    def close(self, timeout: float = 2.0) -> bool:
        self.closed = True
        return True


class FakeClock:
    def __init__(self, start: float = 1_760_000_000.0) -> None:
        self.t = start
        self.m = 100.0

    def now(self) -> float:
        return self.t

    def monotonic(self) -> float:
        return self.m

    def advance(self, seconds: float) -> None:
        self.t += seconds
        self.m += seconds


class SeqIds:
    def __init__(self) -> None:
        import uuid

        self._uuid = uuid

    def uuid(self) -> str:
        return str(self._uuid.uuid4())


class FixedRandom:
    def __init__(self, value: float = 0.5) -> None:
        self.value = value

    def random(self) -> float:
        return self.value


@dataclass
class Harness:
    hub: Hub
    client: FaultlineClient
    transport: FakeTransport
    clock: FakeClock
    rand: FixedRandom

    @property
    def events(self) -> List[Event]:
        return self.transport.events

    @property
    def last(self) -> Event:
        assert self.transport.events, "no event captured"
        return self.transport.events[-1]


def make_harness(**options: Any) -> Harness:
    base: Dict[str, Any] = {"api_key": "flt_ingest_test", "endpoint": "http://127.0.0.1:9"}
    base.update(options)
    opts = ResolvedOptions(**base)
    transport = FakeTransport()
    clock = FakeClock()
    rand = FixedRandom()
    client = FaultlineClient(
        opts,
        ClientDeps(
            transport=transport,
            stack=TracebackStackExtractor(),
            source=LinecacheSourceReader(),
            clock=clock,
            ids=SeqIds(),
            random=rand,
            logger=StderrLogger(False),
            runtime=PythonRuntimeInfo(),
            sdk_version="0.0.0-test",
        ),
    )
    hub = Hub(clock_now=clock.now)
    hub.bind_client(client)
    return Harness(hub=hub, client=client, transport=transport, clock=clock, rand=rand)


@pytest.fixture
def harness() -> Harness:
    return make_harness()


@pytest.fixture(autouse=True)
def _reset_global_sdk() -> Iterator[None]:
    yield
    faultline.close(0.1)
    hub = faultline.application.get_hub()
    hub.root_scope.clear()


# -- local ingest server --------------------------------------------------------------------


@dataclass
class Received:
    path: str
    headers: Dict[str, str]
    raw: bytes
    body: Dict[str, Any]
    at: float


@dataclass
class IngestServer:
    url: str
    received: List[Received] = field(default_factory=list)
    #: queue of (status, headers, body) responses; when empty, 202 is returned.
    responses: List[Tuple[int, Dict[str, str], bytes]] = field(default_factory=list)
    delay: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)
    on_request: Optional[Callable[[Received], None]] = None

    @property
    def events(self) -> List[Dict[str, Any]]:
        return [e for r in self.received for e in r.body.get("events", [])]

    def respond(
        self, status: int, headers: Optional[Dict[str, str]] = None, body: bytes = b""
    ) -> None:
        self.responses.append((status, headers or {}, body))

    def wait_for(self, n: int, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + timeout
        while len(self.received) < n and time.monotonic() < deadline:
            time.sleep(0.01)
        assert len(self.received) >= n, f"expected {n} requests, got {len(self.received)}"


@pytest.fixture
def ingest() -> Iterator[IngestServer]:
    state = IngestServer(url="")

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            data = gzip.decompress(raw) if self.headers.get("Content-Encoding") == "gzip" else raw
            rec = Received(
                path=self.path,
                headers={k.lower(): v for k, v in self.headers.items()},
                raw=raw,
                body=json.loads(data or b"{}"),
                at=time.monotonic(),
            )
            if state.delay:
                time.sleep(state.delay)
            with state.lock:
                state.received.append(rec)
                resp = state.responses.pop(0) if state.responses else None
            if resp is None:
                n = len(rec.body.get("events", []))
                resp = (202, {}, json.dumps({"accepted": n, "rejected": []}).encode())
            status, headers, body = resp
            try:
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            except OSError:
                pass

        def do_GET(self) -> None:
            body = b"hello"
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    state.url = f"http://127.0.0.1:{server.server_port}"
    try:
        yield state
    finally:
        faultline.close(0.5)  # deliver anything pending before the server goes away
        server.shutdown()
        server.server_close()
