"""Pure event processing: contract sanitization, scrubbing, size limits, dedupe, ignore rules."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Sequence, Tuple, cast

from .event import (
    PLATFORM,
    SDK_NAME,
    Breadcrumb,
    Event,
    ExceptionPayload,
    RequestData,
    StackFrame,
    User,
    is_breadcrumb_type,
    is_level,
)
from .limits import (
    CONTEXT_LINES,
    MAX_BREADCRUMBS,
    MAX_CONTEXT_LINE_LENGTH,
    MAX_EXCEPTION_DEPTH,
    MAX_FINGERPRINT_PARTS,
    MAX_FRAMES,
    MAX_MESSAGE_LENGTH,
    MAX_STRING_MAP_ENTRIES,
    MAX_STRING_MAP_KEY_LENGTH,
    MAX_STRING_MAP_VALUE_LENGTH,
    MAX_TAG_KEY_LENGTH,
    MAX_TAG_VALUE_LENGTH,
    MAX_TAGS,
    truncate,
)
from .normalize import to_json_safe, to_json_safe_record, to_string_record
from .options import IgnorePattern
from .scrub import FILTERED, PII_HEADERS, is_sensitive_key, scrub_url, scrub_value


def _opt_str(v: object, max_length: int = MAX_MESSAGE_LENGTH) -> Optional[str]:
    return truncate(v, max_length) if isinstance(v, str) else None


def _pos_int(v: object) -> Optional[int]:
    return v if isinstance(v, int) and not isinstance(v, bool) and v >= 0 else None


def _lines(v: object, keep_last: bool) -> Optional[List[str]]:
    if not isinstance(v, (list, tuple)):
        return None
    lines = [truncate(x, MAX_CONTEXT_LINE_LENGTH) for x in v if isinstance(x, str)]
    return lines[-CONTEXT_LINES:] if keep_last else lines[:CONTEXT_LINES]


def _sanitize_frame(raw: object) -> Optional[StackFrame]:
    if not isinstance(raw, Mapping):
        return None
    filename = raw.get("filename")
    frame: StackFrame = {
        "filename": truncate(filename, 1024)
        if isinstance(filename, str) and filename
        else "<unknown>",
        "inApp": raw.get("inApp") is True,
    }
    fn = _opt_str(raw.get("function"), 1024)
    if fn is not None:
        frame["function"] = fn
    mod = _opt_str(raw.get("module"), 1024)
    if mod is not None:
        frame["module"] = mod
    lineno = _pos_int(raw.get("lineno"))
    if lineno is not None:
        frame["lineno"] = lineno
    colno = _pos_int(raw.get("colno"))
    if colno is not None:
        frame["colno"] = colno
    pre = _lines(raw.get("preContext"), keep_last=True)
    if pre is not None:
        frame["preContext"] = pre
    line = raw.get("contextLine")
    if isinstance(line, str):
        frame["contextLine"] = truncate(line, MAX_CONTEXT_LINE_LENGTH)
    post = _lines(raw.get("postContext"), keep_last=False)
    if post is not None:
        frame["postContext"] = post
    return frame


def _sanitize_exception(raw: object, depth: int) -> Optional[ExceptionPayload]:
    if not isinstance(raw, Mapping) or depth >= MAX_EXCEPTION_DEPTH:
        return None
    t = raw.get("type")
    v = raw.get("value")
    st = raw.get("stacktrace")
    raw_frames = st.get("frames") if isinstance(st, Mapping) else None
    frames: List[StackFrame] = []
    if isinstance(raw_frames, (list, tuple)):
        for f in raw_frames[-MAX_FRAMES:]:
            sf = _sanitize_frame(f)
            if sf is not None:
                frames.append(sf)
    out: ExceptionPayload = {
        "type": truncate(t, 256) if isinstance(t, str) and t else "Error",
        "value": truncate(v, MAX_MESSAGE_LENGTH) if isinstance(v, str) else "",
        "stacktrace": {"frames": frames},
    }
    cause = _sanitize_exception(raw.get("cause"), depth + 1)
    if cause is not None:
        out["cause"] = cause
    return out


def _sanitize_breadcrumb(raw: object, now_iso: str) -> Optional[Breadcrumb]:
    if not isinstance(raw, Mapping):
        return None
    ts = raw.get("timestamp")
    t = raw.get("type")
    crumb: Breadcrumb = {
        "timestamp": ts if isinstance(ts, str) else now_iso,
        "type": t if isinstance(t, str) and is_breadcrumb_type(t) else "default",
    }
    category = _opt_str(raw.get("category"), 256)
    if category is not None:
        crumb["category"] = category
    message = _opt_str(raw.get("message"))
    if message is not None:
        crumb["message"] = message
    level = raw.get("level")
    if isinstance(level, str) and is_level(level):
        crumb["level"] = level
    data = to_json_safe_record(raw.get("data"))
    if data:
        crumb["data"] = data
    return crumb


def _string_map(raw: object) -> Optional[Dict[str, str]]:
    rec = to_string_record(raw)
    if rec is None:
        return None
    out: Dict[str, str] = {}
    for k, v in list(rec.items())[:MAX_STRING_MAP_ENTRIES]:
        key = k[:MAX_STRING_MAP_KEY_LENGTH]
        if key:
            out[key] = truncate(v, MAX_STRING_MAP_VALUE_LENGTH)
    return out


def _sanitize_tags(raw: object) -> Dict[str, str]:
    rec = to_string_record(raw) or {}
    out: Dict[str, str] = {}
    for k, v in rec.items():
        if len(out) >= MAX_TAGS:
            break
        key = k[:MAX_TAG_KEY_LENGTH]
        if key:
            out[key] = truncate(v, MAX_TAG_VALUE_LENGTH)
    return out


def sanitize_event(raw: object, sdk_version: str, now_iso: str) -> Optional[Event]:
    """Coerce an event (possibly returned by a user's ``before_send``) into a payload that
    satisfies the contract: valid enums, required fields present, every pinned limit enforced.
    Returns ``None`` when the input is not salvageable."""
    if not isinstance(raw, Mapping):
        return None
    event_id = raw.get("eventId")
    if not isinstance(event_id, str) or not event_id:
        return None
    ts = raw.get("timestamp")
    level = raw.get("level")
    event: Event = {
        "eventId": event_id,
        "timestamp": ts if isinstance(ts, str) and ts else now_iso,
        "level": level if isinstance(level, str) and is_level(level) else "error",
        "platform": PLATFORM,
        "sdk": {"name": SDK_NAME, "version": sdk_version},
    }
    for key in ("environment", "release", "serverName"):
        s = _opt_str(raw.get(key), 200)
        if s is not None:
            event[key] = s
    message = _opt_str(raw.get("message"))
    if message is not None:
        event["message"] = message

    exception = _sanitize_exception(raw.get("exception"), 0)
    if exception is not None:
        event["exception"] = exception

    fp = raw.get("fingerprint")
    if isinstance(fp, (list, tuple)):
        parts = [
            truncate(str(x), 512)
            for x in fp
            if isinstance(x, (str, int, float)) and not isinstance(x, bool)
        ]
        if parts:
            event["fingerprint"] = parts[:MAX_FINGERPRINT_PARTS]

    tags = _sanitize_tags(raw.get("tags"))
    if tags:
        event["tags"] = tags

    u = raw.get("user")
    if isinstance(u, Mapping):
        user: Dict[str, str] = {}
        for key in ("id", "email", "username", "ipAddress"):
            uv = u.get(key)
            if isinstance(uv, str):
                user[key] = truncate(uv, 64 if key == "ipAddress" else 256)
            elif isinstance(uv, int) and not isinstance(uv, bool):
                user[key] = str(uv)
        if user:
            event["user"] = cast(User, user)

    r = raw.get("request")
    if isinstance(r, Mapping):
        request: RequestData = {}
        method = _opt_str(r.get("method"), 16)
        if method is not None:
            request["method"] = method
        url = _opt_str(r.get("url"), 8192)
        if url is not None:
            request["url"] = url
        headers = _string_map(r.get("headers"))
        if headers is not None:
            request["headers"] = headers
        query = _string_map(r.get("query"))
        if query is not None:
            request["query"] = query
        if request:
            event["request"] = request

    contexts = to_json_safe_record(raw.get("contexts"))
    if contexts:
        event["contexts"] = contexts

    crumbs_raw = raw.get("breadcrumbs")
    if isinstance(crumbs_raw, (list, tuple)):
        crumbs = [
            c
            for c in (_sanitize_breadcrumb(b, now_iso) for b in crumbs_raw[-MAX_BREADCRUMBS:])
            if c is not None
        ]
        if crumbs:
            event["breadcrumbs"] = crumbs

    extra = to_json_safe_record(raw.get("extra"))
    if extra:
        event["extra"] = extra
    return event


def scrub_event(event: Event, send_default_pii: bool) -> Event:
    """Client-side scrubbing (CONTRACTS.md section 2).

    Sensitive keys anywhere in request/extra/contexts/tags/breadcrumb data become
    ``"[Filtered]"``; credit-card values in request/extra/breadcrumb data become ``"[Filtered]"``.
    When ``send_default_pii`` is false, the user IP and cookie/auth/forwarding headers are removed.
    """
    out: Event = cast(Event, dict(event))

    if "request" in out:
        req: RequestData = cast(RequestData, dict(out["request"]))
        if "headers" in req:
            headers: Dict[str, str] = {}
            for k, v in req["headers"].items():
                if not send_default_pii and k.lower() in PII_HEADERS:
                    continue
                headers[k] = FILTERED if is_sensitive_key(k) else v
            req["headers"] = headers
        if "query" in req:
            req["query"] = scrub_value(req["query"], credit_cards=True)
        if "url" in req:
            req["url"] = scrub_url(req["url"])
        out["request"] = req

    if "user" in out:
        user = cast(User, dict(out["user"]))
        if not send_default_pii:
            user.pop("ipAddress", None)
        if user:
            out["user"] = user
        else:
            del out["user"]

    if "tags" in out:
        out["tags"] = {k: FILTERED if is_sensitive_key(k) else v for k, v in out["tags"].items()}
    if "extra" in out:
        out["extra"] = scrub_value(out["extra"], credit_cards=True)
    if "contexts" in out:
        out["contexts"] = scrub_value(out["contexts"], credit_cards=False)
    if "breadcrumbs" in out:
        crumbs: List[Breadcrumb] = []
        for b in out["breadcrumbs"]:
            if "data" in b:
                nb = cast(Breadcrumb, dict(b))
                nb["data"] = scrub_value(b["data"], credit_cards=True)
                crumbs.append(nb)
            else:
                crumbs.append(b)
        out["breadcrumbs"] = crumbs
    return out


def to_json_bytes(value: Any) -> bytes:
    return json.dumps(
        to_json_safe(value, 32, 10_000), ensure_ascii=False, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _strip_context(ex: ExceptionPayload) -> ExceptionPayload:
    frames: List[StackFrame] = []
    for f in ex["stacktrace"]["frames"]:
        nf = cast(StackFrame, {k: v for k, v in f.items() if k not in _CONTEXT_KEYS})
        frames.append(nf)
    out: ExceptionPayload = {
        "type": ex["type"],
        "value": ex["value"],
        "stacktrace": {"frames": frames},
    }
    if "cause" in ex:
        out["cause"] = _strip_context(ex["cause"])
    return out


_CONTEXT_KEYS = frozenset({"preContext", "contextLine", "postContext"})


def serialize_within_limit(event: Event, max_bytes: int) -> Optional[Tuple[Event, bytes]]:
    """Serialize ``event`` so it fits in ``max_bytes``, trimming breadcrumbs, then source context,
    then extra. Returns ``None`` if it still does not fit."""

    def attempt(e: Event) -> Optional[Tuple[Event, bytes]]:
        data = to_json_bytes(e)
        return (e, data) if len(data) <= max_bytes else None

    first = attempt(event)
    if first is not None:
        return first
    current = cast(Event, dict(event))
    current.pop("breadcrumbs", None)
    second = attempt(current)
    if second is not None:
        return second
    if "exception" in current:
        current["exception"] = _strip_context(current["exception"])
    third = attempt(current)
    if third is not None:
        return third
    current.pop("extra", None)
    return attempt(current)


def dedupe_key(event: Event) -> str:
    """Stable key used to detect identical consecutive events (client-side dedupe)."""
    fp = event.get("fingerprint")
    if fp:
        return "fp:" + "|".join(fp)
    ex = event.get("exception")
    if ex is not None:
        frames = ",".join(
            f"{f['filename']}:{f.get('function', '?')}:{f.get('lineno', 0)}:{f.get('colno', 0)}"
            for f in ex["stacktrace"]["frames"][-5:]
        )
        return f"ex:{ex['type']}:{ex['value']}:{frames}"
    return f"msg:{event['level']}:{event.get('message', '')}"


def matches_ignore_errors(
    event: Event,
    patterns: Sequence[IgnorePattern],
    exception: Optional[BaseException] = None,
) -> bool:
    """``ignore_errors`` matching.

    Strings match as substrings and compiled patterns are searched, both against the exception
    type, the message and ``"Type: message"``. Exception classes match with ``isinstance``.
    """
    if not patterns:
        return False
    candidates: List[str] = []
    ex = event.get("exception")
    if ex is not None:
        candidates += [ex["type"], ex["value"], f"{ex['type']}: {ex['value']}"]
    elif "message" in event:
        candidates.append(event["message"])
    for p in patterns:
        if isinstance(p, type):
            if exception is not None and isinstance(exception, p):
                return True
            continue
        for c in candidates:
            if isinstance(p, str):
                if p in c:
                    return True
            elif isinstance(p, re.Pattern) and p.search(c):
                return True
    return False
