"""Client behavior: sampling, ignore_errors, dedupe, before_send, limits, conformance."""

from __future__ import annotations

import re
from typing import Any, Optional

from faultline.application import CaptureHint
from faultline.domain import Event
from tests.conftest import make_harness

from .contract import assert_conforms


def test_message_event_shape_conforms() -> None:
    h = make_harness(environment="production", release="api@1.4.2", server_name="web-1")
    h.hub.set_tag("region", "eu")
    h.hub.set_user({"id": "u1", "email": "a@b.c"})
    h.hub.set_context("order", {"id": 7})
    h.hub.add_breadcrumb({"type": "navigation", "message": "to /cart", "level": "info"})
    event_id = h.hub.capture_message("Payment webhook retried", "warning")
    ev = h.last
    assert event_id == ev["eventId"]
    assert ev["level"] == "warning"
    assert ev["platform"] == "python"
    assert ev["sdk"] == {"name": "getfaultline", "version": "0.0.0-test"}
    assert ev["environment"] == "production"
    assert ev["release"] == "api@1.4.2"
    assert ev["serverName"] == "web-1"
    assert ev["tags"] == {"region": "eu"}
    assert ev["user"] == {"id": "u1", "email": "a@b.c"}
    assert ev["contexts"]["order"] == {"id": 7}
    assert ev["contexts"]["runtime"]["name"] == "python"
    assert ev["breadcrumbs"][0]["type"] == "navigation"
    assert ev["timestamp"].endswith("Z")
    assert_conforms(ev)


def test_capture_hint_overrides_scope() -> None:
    h = make_harness()
    h.hub.set_tag("a", "scope")
    h.hub.get_scope().set_level("warning")
    h.hub.capture_exception(
        ValueError("x"),
        CaptureHint(
            tags={"a": "hint", "b": "2"}, extra={"k": 1}, fingerprint=["custom"], level="fatal"
        ),
    )
    ev = h.last
    assert ev["tags"] == {"a": "hint", "b": "2"}
    assert ev["extra"] == {"k": 1}
    assert ev["fingerprint"] == ["custom"]
    assert ev["level"] == "fatal"


def test_sample_rate() -> None:
    h = make_harness(sample_rate=0.25)
    h.rand.value = 0.3
    assert h.hub.capture_message("dropped") is None
    h.rand.value = 0.1
    assert h.hub.capture_message("kept") is not None
    assert [e["message"] for e in h.events] == ["kept"]


def test_ignore_errors_strings_patterns_and_classes() -> None:
    class Custom(Exception):
        pass

    h = make_harness(ignore_errors=("ECONNRESET", re.compile(r"^Timeout"), Custom))
    assert h.hub.capture_exception(OSError("read ECONNRESET")) is None
    assert h.hub.capture_exception(TimeoutError("x")) is None  # matches "TimeoutError: x"
    assert h.hub.capture_exception(Custom("y")) is None
    assert h.hub.capture_message("ECONNRESET happened") is None
    assert h.hub.capture_exception(ValueError("fine")) is not None
    assert len(h.events) == 1


def test_identical_consecutive_errors_are_deduped_within_one_second() -> None:
    h = make_harness()

    def fail() -> None:
        raise ValueError("same")

    def capture() -> Optional[str]:
        try:
            fail()
        except ValueError as exc:
            return h.hub.capture_exception(exc)
        return None

    assert capture() is not None
    h.clock.advance(0.5)
    assert capture() is None
    h.clock.advance(0.6)
    assert capture() is not None
    h.hub.capture_message("other")
    assert capture() is not None  # not consecutive any more
    assert len(h.events) == 4


def test_before_send_can_modify_or_drop() -> None:
    def before_send(event: Event) -> Optional[Event]:
        if event.get("message") == "drop me":
            return None
        event["tags"] = {"modified": "yes"}
        return event

    h = make_harness(before_send=before_send)
    assert h.hub.capture_message("drop me") is None
    assert h.hub.capture_message("keep me") is not None
    assert h.last["tags"] == {"modified": "yes"}


def test_before_send_output_is_resanitized() -> None:
    def before_send(event: Event) -> Any:
        event["level"] = "nonsense"  # type: ignore[typeddict-item]
        event["tags"] = {f"k{i}": "v" * 500 for i in range(80)}
        event["unknown"] = True  # type: ignore[typeddict-unknown-key]
        return event

    h = make_harness(before_send=before_send)
    h.hub.capture_message("x")
    ev = h.last
    assert ev["level"] == "error"
    assert len(ev["tags"]) == 50 and all(len(v) == 200 for v in ev["tags"].values())
    assert "unknown" not in ev
    assert_conforms(ev)


def test_before_send_raising_drops_the_event() -> None:
    def before_send(event: Event) -> Event:
        raise RuntimeError("bug in hook")

    h = make_harness(before_send=before_send)
    assert h.hub.capture_message("x") is None
    assert h.events == []


def test_breadcrumbs_respect_max_breadcrumbs() -> None:
    h = make_harness(max_breadcrumbs=3)
    for i in range(10):
        h.hub.add_breadcrumb({"message": f"b{i}"})
    h.hub.capture_message("x")
    assert [b["message"] for b in h.last["breadcrumbs"]] == ["b7", "b8", "b9"]


def test_extra_with_unserializable_values() -> None:
    h = make_harness()
    cyclic: dict[str, Any] = {}
    cyclic["self"] = cyclic
    h.hub.set_extra("cyclic", cyclic)
    h.hub.set_extra("obj", object())
    h.hub.set_extra("bytes", b"\x00\x01")
    h.hub.set_extra("nan", float("nan"))
    h.hub.set_extra("set", {3, 1, 2})
    h.hub.capture_message("x")
    extra = h.last["extra"]
    assert extra["cyclic"] == {"self": "[Circular]"}
    assert extra["obj"].startswith("<object object")
    assert extra["bytes"] == "<bytes len=2>"
    assert extra["nan"] == "nan"
    assert extra["set"] == [1, 2, 3]
    assert_conforms(h.last)


def test_disabled_and_closed_clients_capture_nothing() -> None:
    h = make_harness(enabled=False)
    assert h.hub.capture_message("x") is None
    h2 = make_harness()
    h2.client.close()
    assert h2.hub.capture_message("x") is None
    assert h2.transport.closed
