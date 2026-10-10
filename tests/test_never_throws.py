"""The SDK must never raise into the host application."""

from __future__ import annotations

from typing import Any

import pytest

import faultline
from faultline.application import CaptureHint
from faultline.domain import resolve_options
from tests.conftest import IngestServer, make_harness


class Hostile:
    def __repr__(self) -> str:
        raise RuntimeError("repr")

    def __str__(self) -> str:
        raise RuntimeError("str")

    def __eq__(self, other: object) -> bool:
        raise RuntimeError("eq")

    __hash__ = object.__hash__


def call_everything(arg: Any) -> None:
    faultline.capture_exception(arg)
    faultline.capture_exception(arg, tags=arg, extra=arg, user=arg, level=arg, fingerprint=arg)
    faultline.capture_message(arg, arg)
    faultline.set_user(arg)
    faultline.set_tag(arg, arg)
    faultline.set_tags(arg)
    faultline.set_context(arg, arg)
    faultline.set_extra(arg, arg)
    faultline.add_breadcrumb(arg)
    faultline.add_breadcrumb(message=arg, data=arg, level=arg, type=arg)
    faultline.flush(arg)
    with faultline.with_scope():
        pass


BAD_VALUES = [None, 1, "x", Hostile(), {"a": Hostile()}, [Hostile()], object(), float("nan")]


@pytest.mark.parametrize("arg", BAD_VALUES)
def test_public_api_before_init_never_raises(arg: Any) -> None:
    call_everything(arg)


@pytest.mark.parametrize("arg", BAD_VALUES)
def test_public_api_after_init_never_raises(arg: Any, ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url)
    call_everything(arg)
    faultline.capture_exception(ValueError("x"), unexpected_kwarg=1)
    faultline.capture_message("x", unexpected=True)


@pytest.mark.parametrize(
    "options",
    [
        {},
        {"api_key": 42},
        {"api_key": "k", "endpoint": "ftp://nope"},
        {"api_key": "k", "endpoint": "not a url"},
        {"api_key": "k", "sample_rate": "all"},
        {"api_key": "k", "max_breadcrumbs": -1},
        {"api_key": "k", "before_send": "nope"},
        {"api_key": "k", "ignore_errors": "nope"},
        {"api_key": "k", "ignore_errors": [1, None]},
        {"api_key": "k", "integrations": {"nope": True, "logging": "maybe"}},
        {"api_key": "k", "integrations": 5},
        {"api_key": "k", "debug": "yes"},
        {"api_key": "k", "totally_unknown": 1},
    ],
)
def test_init_with_invalid_options_never_raises(options: Any) -> None:
    faultline.init(**{"endpoint": "http://127.0.0.1:9", **options})  # never hit the network
    faultline.capture_message("still fine")


def test_invalid_options_fall_back_to_defaults() -> None:
    r = resolve_options({"api_key": "k", "sample_rate": 3, "max_breadcrumbs": 500})
    assert r.options is not None
    assert r.options.sample_rate == 1.0
    assert r.options.max_breadcrumbs == 100
    assert len(r.warnings) == 2
    missing = resolve_options({})
    assert missing.options is None and missing.errors == ['"api_key" is required']
    disabled = resolve_options({"enabled": False})
    assert disabled.options is not None and not disabled.options.enabled


def test_failing_dependencies_are_contained() -> None:
    h = make_harness()

    def boom(*_: Any, **__: Any) -> Any:
        raise RuntimeError("dependency failure")

    h.transport.send = boom  # type: ignore[method-assign]
    h.client._deps.runtime.contexts = boom  # type: ignore[method-assign]
    assert h.hub.capture_exception(ValueError("x"), CaptureHint()) is None
    assert h.hub.capture_message("x") is None
    h.transport.flush = boom  # type: ignore[method-assign]
    assert h.client.flush(0.1) is False


def test_capture_after_close_and_double_close(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url)
    assert faultline.is_enabled()
    assert faultline.close(1.0)
    assert faultline.close(1.0)
    assert not faultline.is_enabled()
    assert faultline.capture_message("after close") is None


def test_unreachable_endpoint_never_raises_and_flush_times_out() -> None:
    faultline.init(api_key="flt_ingest_test", endpoint="http://127.0.0.1:9")
    faultline.capture_message("nobody listens")
    assert faultline.flush(0.3) is False


def test_capture_exception_without_active_exception_returns_none(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url)
    assert faultline.capture_exception() is None


def test_capture_exception_defaults_to_sys_exc_info(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url)
    try:
        raise LookupError("current")
    except LookupError:
        event_id = faultline.capture_exception()
        info_id = None
        import sys

        info_id = faultline.capture_exception(sys.exc_info(), tags={"via": "tuple"})
    assert event_id is not None
    assert info_id is None  # identical consecutive error within 1 s is deduped
    assert faultline.flush(5)
    assert ingest.events[0]["exception"]["type"] == "LookupError"
