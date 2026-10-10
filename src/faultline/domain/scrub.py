"""Client-side PII scrubbing.

The regexes are copied from CONTRACTS.md section 2, so the SDK and the server agree on what is
sensitive. ``re.ASCII`` keeps ``\\d`` and ``\\b`` identical to the JavaScript semantics.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any, Dict
from urllib.parse import unquote

FILTERED = "[Filtered]"

SENSITIVE_KEY_REGEX = re.compile(
    r"(authorization|cookie|set-cookie|password|passwd|secret|token|api[_-]?key|x-faultline-key)",
    re.IGNORECASE | re.ASCII,
)

CREDIT_CARD_REGEX = re.compile(r"\b(?:\d[ -]*?){13,16}\b", re.ASCII)

#: Header names removed entirely when ``send_default_pii`` is false.
PII_HEADERS = frozenset(
    {
        "cookie",
        "set-cookie",
        "authorization",
        "proxy-authorization",
        "x-forwarded-for",
        "x-real-ip",
        "forwarded",
    }
)

_URL_PARAM = re.compile(r"([?&])([^=&#]+)=([^&#]*)")


def is_sensitive_key(key: str) -> bool:
    return SENSITIVE_KEY_REGEX.search(key) is not None


def looks_like_credit_card(value: str) -> bool:
    return CREDIT_CARD_REGEX.search(value) is not None


def scrub_url(value: str) -> str:
    """Mask sensitive query parameters in a URL (`?token=abc` becomes `?token=[Filtered]`)."""
    if "?" not in value or "=" not in value:
        return value

    def repl(match: re.Match[str]) -> str:
        sep, key = match.group(1), match.group(2)
        try:
            decoded = unquote(key, errors="strict")
        except Exception:
            decoded = key
        return f"{sep}{key}={FILTERED}" if is_sensitive_key(decoded) else match.group(0)

    return _URL_PARAM.sub(repl, value)


def scrub_value(value: Any, credit_cards: bool, depth: int = 0) -> Any:
    """Deep-scrub a JSON-safe value and return a new value; never mutates the input.

    Values under sensitive keys become ``"[Filtered]"``. With ``credit_cards``, card-like strings
    become ``"[Filtered]"`` and URLs get sensitive query parameters masked.
    """
    if depth > 20:
        return value
    if isinstance(value, str):
        if credit_cards:
            return FILTERED if looks_like_credit_card(value) else scrub_url(value)
        return value
    if isinstance(value, (list, tuple)):
        return [scrub_value(v, credit_cards, depth + 1) for v in value]
    if isinstance(value, Mapping):
        out: Dict[str, Any] = {}
        for k, v in value.items():
            key = str(k)
            out[key] = (
                FILTERED if is_sensitive_key(key) else scrub_value(v, credit_cards, depth + 1)
            )
        return out
    return value
