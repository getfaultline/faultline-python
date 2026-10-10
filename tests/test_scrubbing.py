"""Scrubbing: identical rules to the Node SDK and the server (CONTRACTS.md section 2)."""

from __future__ import annotations

from typing import Any, Dict

import pytest

from faultline.domain import (
    CREDIT_CARD_REGEX,
    FILTERED,
    SENSITIVE_KEY_REGEX,
    Event,
    scrub_event,
    scrub_url,
    scrub_value,
)
from tests.conftest import make_harness


def base() -> Dict[str, Any]:
    return {
        "eventId": "00000000-0000-4000-8000-000000000000",
        "timestamp": "2026-01-01T00:00:00.000Z",
        "level": "error",
        "platform": "python",
        "sdk": {"name": "getfaultline", "version": "0"},
    }


def test_uses_the_exact_regexes_from_contracts() -> None:
    assert SENSITIVE_KEY_REGEX.pattern == (
        "(authorization|cookie|set-cookie|password|passwd|secret|token|api[_-]?key|x-faultline-key)"
    )
    assert CREDIT_CARD_REGEX.pattern == r"\b(?:\d[ -]*?){13,16}\b"


@pytest.mark.parametrize(
    "key",
    [
        "Authorization",
        "COOKIE",
        "set-cookie",
        "password",
        "user_passwd",
        "clientSecret",
        "accessToken",
        "api_key",
        "apiKey",
        "API-KEY",
        "X-Faultline-Key",
    ],
)
def test_filters_values_under_sensitive_keys(key: str) -> None:
    assert scrub_value({key: "s3cr3t", "safe": "ok"}, credit_cards=True) == {
        key: FILTERED,
        "safe": "ok",
    }


def test_nested_keys_credit_cards_and_urls() -> None:
    out = scrub_value(
        {
            "a": {"b": [{"password": "x"}, "4111 1111 1111 1111"]},
            "url": "https://x.io/cb?token=abc&page=2",
            "n": 5,
        },
        credit_cards=True,
    )
    assert out == {
        "a": {"b": [{"password": FILTERED}, FILTERED]},
        "url": f"https://x.io/cb?token={FILTERED}&page=2",
        "n": 5,
    }
    assert scrub_url("/x?api_key=1&q=hello") == f"/x?api_key={FILTERED}&q=hello"
    assert scrub_url("/x?api%5Fkey=1") == f"/x?api%5Fkey={FILTERED}"
    assert scrub_url("/plain/path") == "/plain/path"


@pytest.mark.parametrize(
    ("value", "filtered"),
    [
        ("4111111111111111", True),
        ("5500-0000-0000-0004", True),
        ("card 4111 1111 1111 1111 here", True),
        ("123456789012", False),  # 12 digits
        ("order 42", False),
        ("٤١١١١١١١١١١١١١١١", False),
    ],
)
def test_credit_card_detection_matches_javascript_semantics(value: str, filtered: bool) -> None:
    assert (scrub_value(value, credit_cards=True) == FILTERED) is filtered


def test_scrub_event_without_default_pii() -> None:
    event: Event = {  # type: ignore[typeddict-item]
        **base(),
        "request": {
            "url": "http://api/x?secret=1",
            "headers": {
                "Authorization": "Bearer t",
                "Cookie": "sid=1",
                "X-Forwarded-For": "1.2.3.4",
                "x-custom-token": "t",
                "accept": "json",
            },
            "query": {"card": "5500-0000-0000-0004", "q": "ok"},
        },
        "user": {"id": "u1", "ipAddress": "1.2.3.4"},
        "tags": {"auth_token": "t", "region": "eu"},
        "extra": {"password": "p", "nested": {"cc": "4111111111111111"}},
        "contexts": {"db": {"password": "p", "host": "h"}},
        "breadcrumbs": [
            {"timestamp": "t", "type": "http", "data": {"url": "/?token=1", "apiKey": "k"}}
        ],
    }
    out = scrub_event(event, send_default_pii=False)
    assert out["request"]["headers"] == {"x-custom-token": FILTERED, "accept": "json"}
    assert out["request"]["query"] == {"card": FILTERED, "q": "ok"}
    assert out["request"]["url"] == f"http://api/x?secret={FILTERED}"
    assert out["user"] == {"id": "u1"}
    assert out["tags"] == {"auth_token": FILTERED, "region": "eu"}
    assert out["extra"] == {"password": FILTERED, "nested": {"cc": FILTERED}}
    assert out["contexts"] == {"db": {"password": FILTERED, "host": "h"}}
    assert out["breadcrumbs"][0]["data"] == {"url": f"/?token={FILTERED}", "apiKey": FILTERED}
    # input not mutated
    assert event["user"]["ipAddress"] == "1.2.3.4"
    assert event["request"]["headers"]["Cookie"] == "sid=1"


def test_scrub_event_with_default_pii_keeps_ip_but_still_filters() -> None:
    event: Event = {  # type: ignore[typeddict-item]
        **base(),
        "request": {"headers": {"cookie": "sid=1", "x-forwarded-for": "1.2.3.4"}},
        "user": {"ipAddress": "1.2.3.4"},
    }
    out = scrub_event(event, send_default_pii=True)
    assert out["request"]["headers"] == {"cookie": FILTERED, "x-forwarded-for": "1.2.3.4"}
    assert out["user"] == {"ipAddress": "1.2.3.4"}


def test_contexts_are_not_credit_card_scrubbed() -> None:
    event: Event = {**base(), "contexts": {"order": {"ref": "4111111111111111"}}}  # type: ignore[typeddict-item]
    assert scrub_event(event, False)["contexts"] == {"order": {"ref": "4111111111111111"}}


def test_client_scrubs_before_before_send_sees_the_event() -> None:
    seen: Dict[str, Any] = {}

    def before_send(event: Event) -> Event:
        seen.update(event)
        return event

    h = make_harness(before_send=before_send)
    h.hub.capture_message("hi", hint=None)
    h.hub.set_extra("password", "p")
    h.hub.set_user({"id": "1", "ipAddress": "9.9.9.9"})
    h.clock.advance(2)
    h.hub.capture_message("again")
    assert seen["extra"] == {"password": FILTERED}
    assert seen["user"] == {"id": "1"}
