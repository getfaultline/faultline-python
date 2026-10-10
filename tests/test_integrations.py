"""Built-in integrations: excepthook, threading, logging, http.client."""

from __future__ import annotations

import logging
import sys
import threading
import urllib.request
from typing import Any, List

import faultline
from faultline import LoggingIntegration
from faultline.integrations import (
    ExcepthookIntegration,
    StdlibHttpIntegration,
    ThreadingIntegration,
)
from tests.conftest import IngestServer, make_harness


def test_sys_excepthook_captures_flushes_and_chains() -> None:
    h = make_harness()
    calls: List[Any] = []
    previous = sys.excepthook
    sys.excepthook = lambda *a: calls.append(a)
    integration = ExcepthookIntegration()
    try:
        integration.setup(h.hub)
        try:
            raise RuntimeError("crash")
        except RuntimeError as exc:
            sys.excepthook(type(exc), exc, exc.__traceback__)
        sys.excepthook(KeyboardInterrupt, KeyboardInterrupt(), None)
    finally:
        integration.teardown()
        restored = sys.excepthook
        sys.excepthook = previous
    assert len(calls) == 2  # previous hook always runs
    assert len(h.events) == 1  # KeyboardInterrupt is not captured
    ev = h.last
    assert ev["level"] == "fatal"
    assert ev["tags"] == {"mechanism": "excepthook", "handled": "no"}
    assert h.transport.flushed == 1
    assert restored is not integration._installed


def test_threading_excepthook() -> None:
    h = make_harness()
    calls: List[Any] = []
    previous = threading.excepthook
    threading.excepthook = lambda args: calls.append(args.exc_type)
    integration = ThreadingIntegration()
    try:
        integration.setup(h.hub)

        def work() -> None:
            raise ValueError("in thread")

        t = threading.Thread(target=work)
        t.start()
        t.join()
    finally:
        integration.teardown()
        threading.excepthook = previous
    assert calls == [ValueError]
    assert h.last["exception"]["type"] == "ValueError"
    assert h.last["tags"]["mechanism"] == "threading"


def test_logging_records_become_breadcrumbs() -> None:
    h = make_harness()
    integration = LoggingIntegration()
    integration.setup(h.hub)
    log = logging.getLogger("tests.app")
    log.setLevel(logging.DEBUG)
    try:
        log.debug("too low")
        log.info("user %s logged in", "ada")
        log.warning("disk at %d%%", 91)
        logging.getLogger("faultline.internal").warning("ignored SDK logger")
        h.hub.capture_message("x")
    finally:
        integration.teardown()
    crumbs = h.last["breadcrumbs"]
    assert [(c["level"], c["message"]) for c in crumbs] == [
        ("info", "user ada logged in"),
        ("warning", "disk at 91%"),
    ]
    assert crumbs[0]["type"] == "log" and crumbs[0]["category"] == "tests.app"
    assert logging.Logger.callHandlers is not integration._wrapper


def test_logging_errors_are_not_captured_by_default() -> None:
    h = make_harness()
    integration = LoggingIntegration()
    integration.setup(h.hub)
    try:
        try:
            1 / 0  # noqa: B018
        except ZeroDivisionError:
            logging.getLogger("tests.app").exception("failed")
    finally:
        integration.teardown()
    assert h.events == []


def test_logging_capture_errors_opt_in() -> None:
    h = make_harness()
    integration = LoggingIntegration(capture_errors=True)
    integration.setup(h.hub)
    try:
        try:
            1 / 0  # noqa: B018
        except ZeroDivisionError:
            logging.getLogger("tests.app").exception("division failed")
        logging.getLogger("tests.app").error("no exc_info, breadcrumb only")
        h.clock.advance(2)
        h.hub.capture_message("after")
    finally:
        integration.teardown()
    first = h.events[0]
    assert first["exception"]["type"] == "ZeroDivisionError"
    assert first["level"] == "error"
    assert first["tags"]["logger"] == "tests.app"
    assert first["extra"]["log.message"] == "division failed"
    assert h.last["breadcrumbs"][-1]["message"] == "no exc_info, breadcrumb only"


def test_logging_with_broken_format_args_never_raises() -> None:
    h = make_harness()
    integration = LoggingIntegration()
    integration.setup(h.hub)
    try:
        record = logging.LogRecord("tests.app", logging.WARNING, __file__, 1, "%d", ("x",), None)
        integration._record(record)  # the hook itself must not raise
        h.hub.capture_message("x")
    finally:
        integration.teardown()
    assert h.last["breadcrumbs"][0]["message"] == "%d"


def test_http_client_breadcrumbs(ingest: IngestServer) -> None:
    h = make_harness(endpoint=ingest.url)
    integration = StdlibHttpIntegration()
    integration.setup(h.hub)
    try:
        with urllib.request.urlopen(f"{ingest.url}/health?token=abc") as res:
            assert res.read() == b"hello"
        try:
            urllib.request.urlopen(f"{ingest.url}/api/v1/ingest/events", data=b"{}")
        except Exception:
            pass
        h.hub.capture_message("x")
    finally:
        integration.teardown()
    crumbs = [c for c in h.last.get("breadcrumbs", []) if c["type"] == "http"]
    assert len(crumbs) == 1  # the ingest URL is skipped
    data = crumbs[0]["data"]
    assert data["method"] == "GET"
    assert data["status_code"] == 200
    assert data["url"] == f"{ingest.url}/health?token=[Filtered]"
    assert crumbs[0]["level"] == "info"


def test_http_client_error_breadcrumb_and_passthrough() -> None:
    h = make_harness()
    integration = StdlibHttpIntegration()
    integration.setup(h.hub)
    try:
        try:
            urllib.request.urlopen("http://127.0.0.1:9/down", timeout=1)
        except OSError:
            pass
        h.hub.capture_message("x")
    finally:
        integration.teardown()
    assert integration._originals is None


def test_init_installs_and_close_removes_integrations(ingest: IngestServer) -> None:
    original_hook = sys.excepthook
    original_call = logging.Logger.callHandlers
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url)
    assert sys.excepthook is not original_hook
    assert logging.Logger.callHandlers is not original_call
    faultline.close(1)
    assert sys.excepthook is original_hook
    assert logging.Logger.callHandlers is original_call


def test_integrations_can_be_disabled_or_configured(ingest: IngestServer) -> None:
    original_hook = sys.excepthook
    configured = LoggingIntegration(capture_errors=True)
    faultline.init(
        api_key="flt_ingest_test",
        endpoint=ingest.url,
        integrations={"excepthook": False, "logging": configured, "http": False},
    )
    assert sys.excepthook is original_hook
    try:
        raise ValueError("logged")
    except ValueError:
        logging.getLogger("tests.cfg").exception("boom")
    assert faultline.flush(5)
    assert ingest.events[0]["exception"]["value"] == "logged"
