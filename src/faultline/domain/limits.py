"""Limits pinned in CONTRACTS.md section 2. The SDK and the server use the same numbers."""

from __future__ import annotations

from datetime import datetime, timezone

MAX_BATCH_EVENTS = 50
MAX_BODY_BYTES = 1024 * 1024
MAX_TAGS = 50
MAX_TAG_KEY_LENGTH = 32
MAX_TAG_VALUE_LENGTH = 200
MAX_BREADCRUMBS = 100
#: Total exception chain length: the root exception plus at most 4 causes (ADR-007).
MAX_EXCEPTION_DEPTH = 5
MAX_FRAMES = 100
MAX_MESSAGE_LENGTH = 8192
CONTEXT_LINES = 5
MAX_CONTEXT_LINE_LENGTH = 300

# Not pinned in CONTRACTS.md, but enforced by the server's ingest schema.
MAX_FINGERPRINT_PARTS = 20
MAX_STRING_MAP_ENTRIES = 200
MAX_STRING_MAP_KEY_LENGTH = 256
MAX_STRING_MAP_VALUE_LENGTH = 8192

ELLIPSIS = "…"


def truncate(value: str, max_length: int) -> str:
    """Truncate to at most ``max_length`` characters, including the trailing ellipsis."""
    if len(value) <= max_length:
        return value
    if max_length <= 0:
        return ""
    return value[: max_length - 1] + ELLIPSIS


def iso_timestamp(epoch_seconds: float) -> str:
    """ISO 8601 UTC timestamp with millisecond precision, e.g. ``2026-10-09T12:00:00.123Z``."""
    dt = datetime.fromtimestamp(epoch_seconds, tz=timezone.utc)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")
