"""Exception building: cause / context chains, groups, non-exception values."""

from __future__ import annotations

import sys
from typing import Any

import pytest

from tests.conftest import Harness

from .contract import assert_conforms


def chain(exc: Any) -> list[tuple[str, str]]:
    out = []
    while exc is not None:
        out.append((exc["type"], exc["value"]))
        exc = exc.get("cause")
    return out


def test_explicit_cause(harness: Harness) -> None:
    try:
        try:
            raise KeyError("missing")
        except KeyError as inner:
            raise RuntimeError("lookup failed") from inner
    except RuntimeError as exc:
        harness.hub.capture_exception(exc)
    ev = harness.last
    assert chain(ev["exception"]) == [("RuntimeError", "lookup failed"), ("KeyError", "'missing'")]
    assert ev["message"] == "lookup failed"
    assert ev["exception"]["cause"]["stacktrace"]["frames"]
    assert_conforms(ev)


def test_implicit_context(harness: Harness) -> None:
    try:
        try:
            1 / 0  # noqa: B018
        except ZeroDivisionError:
            raise ValueError("while handling")  # noqa: B904
    except ValueError as exc:
        harness.hub.capture_exception(exc)
    assert chain(harness.last["exception"])[1][0] == "ZeroDivisionError"


def test_from_none_suppresses_context(harness: Harness) -> None:
    try:
        try:
            1 / 0  # noqa: B018
        except ZeroDivisionError:
            raise ValueError("clean") from None
    except ValueError as exc:
        harness.hub.capture_exception(exc)
    assert "cause" not in harness.last["exception"]


def test_chain_is_capped_at_five_exceptions(harness: Harness) -> None:
    exc: BaseException = RuntimeError("e0")
    for i in range(1, 10):
        nxt = RuntimeError(f"e{i}")
        nxt.__cause__ = exc
        exc = nxt
    harness.hub.capture_exception(exc)
    links = chain(harness.last["exception"])
    assert [v for _, v in links] == ["e9", "e8", "e7", "e6", "e5"]
    assert_conforms(harness.last)


def test_cycles_are_broken(harness: Harness) -> None:
    a, b = ValueError("a"), ValueError("b")
    a.__cause__ = b
    b.__cause__ = a
    harness.hub.capture_exception(a)
    assert [v for _, v in chain(harness.last["exception"])] == ["a", "b"]


@pytest.mark.skipif(sys.version_info < (3, 11), reason="ExceptionGroup needs Python 3.11+")
def test_exception_group_degrades_gracefully(harness: Harness) -> None:
    import builtins

    group_cls: Any = getattr(builtins, "ExceptionGroup")  # noqa: B009
    try:
        raise group_cls("many", [ValueError("one"), TypeError("two")])
    except Exception as exc:
        harness.hub.capture_exception(exc)
    ex = harness.last["exception"]
    assert ex["type"] == "ExceptionGroup"
    assert "ValueError" in ex["value"] and "TypeError" in ex["value"]
    assert ex["cause"]["type"] == "ValueError"
    assert_conforms(harness.last)


def test_non_exception_values_become_synthetic_errors(harness: Harness) -> None:
    harness.hub.capture_exception("plain string")
    ev = harness.last
    assert ev["exception"]["type"] == "Error"
    assert ev["exception"]["value"] == "plain string"
    frames = ev["exception"]["stacktrace"]["frames"]
    assert frames and frames[-1]["function"].endswith(
        "test_non_exception_values_become_synthetic_errors"
    )

    harness.clock.advance(5)
    harness.hub.capture_exception({"code": 42})
    assert "keys: code" in harness.last["exception"]["value"]
    assert_conforms(harness.last)


def test_hostile_exception_str(harness: Harness) -> None:
    class Weird(Exception):
        def __str__(self) -> str:
            raise RuntimeError("nope")

    harness.hub.capture_exception(Weird())
    assert harness.last["exception"]["type"] == "Weird"
    assert_conforms(harness.last)


def test_long_messages_are_truncated(harness: Harness) -> None:
    harness.hub.capture_exception(ValueError("x" * 10_000))
    value = harness.last["exception"]["value"]
    assert len(value) == 8192 and value.endswith("\u2026")


def test_frames_are_capped_at_100_newest(harness: Harness) -> None:
    def recurse(n: int) -> None:
        if n == 0:
            raise RecursionError("bottom")
        recurse(n - 1)

    with pytest.raises(RecursionError) as info:
        recurse(150)
    harness.hub.capture_exception(info.value)
    frames = harness.last["exception"]["stacktrace"]["frames"]
    assert len(frames) == 100
    assert frames[-1]["lineno"] == recurse.__code__.co_firstlineno + 2
