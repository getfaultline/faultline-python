"""Convert arbitrary user values into JSON-safe data.

Handles cycles, non-finite floats, bytes, dates, exceptions, sets and arbitrary objects, and
caps depth and breadth so a single ``extra`` value can never break serialization.
"""

from __future__ import annotations

import datetime as _dt
import math
from collections.abc import Mapping
from typing import Any, Dict, Optional, Set

from .limits import MAX_MESSAGE_LENGTH, truncate


def safe_repr(value: object, max_length: int = 1024) -> str:
    try:
        return truncate(repr(value), max_length)
    except Exception:
        return "<unrepresentable>"


def safe_str(value: object) -> str:
    try:
        return value if isinstance(value, str) else str(value)
    except Exception:
        return safe_repr(value)


def to_json_safe(value: object, max_depth: int = 6, max_breadth: int = 200) -> Any:
    seen: Set[int] = set()

    def walk(v: object, depth: int) -> Any:
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, int):
            return v if abs(v) < 2**63 else str(v)
        if isinstance(v, float):
            return v if math.isfinite(v) else str(v)
        if isinstance(v, str):
            return truncate(v, MAX_MESSAGE_LENGTH)
        if isinstance(v, (bytes, bytearray, memoryview)):
            return f"<{type(v).__name__} len={len(v)}>"
        if isinstance(v, (_dt.datetime, _dt.date, _dt.time)):
            return v.isoformat()
        if isinstance(v, BaseException):
            return {"type": type(v).__name__, "message": truncate(safe_str(v), MAX_MESSAGE_LENGTH)}
        if not isinstance(v, (Mapping, list, tuple, set, frozenset)):
            return safe_repr(v)

        ident = id(v)
        if ident in seen:
            return "[Circular]"
        if depth >= max_depth:
            return "[Object]" if isinstance(v, Mapping) else "[Array]"
        seen.add(ident)
        try:
            if isinstance(v, Mapping):
                out: Dict[str, Any] = {}
                for i, (k, item) in enumerate(list(v.items())):
                    if i >= max_breadth:
                        break
                    out[k if isinstance(k, str) else safe_str(k)] = walk(item, depth + 1)
                return out
            items = list(v)
            if isinstance(v, (set, frozenset)):
                try:
                    items = sorted(items, key=safe_repr)
                except Exception:
                    pass
            return [walk(item, depth + 1) for item in items[:max_breadth]]
        except Exception:
            return "[Unserializable]"
        finally:
            seen.discard(ident)

    return walk(value, 0)


def to_json_safe_record(value: object) -> Optional[Dict[str, Any]]:
    if not isinstance(value, Mapping):
        return None
    result = to_json_safe(value)
    return result if isinstance(result, dict) else None


def _stringify(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, (bytes, bytearray)):
        try:
            return bytes(value).decode("utf-8", "replace")
        except Exception:
            return ""
    if isinstance(value, (int, float, bool)):
        return str(value)
    return safe_str(to_json_safe(value))


def to_string_record(value: object) -> Optional[Dict[str, str]]:
    """Coerce a mapping's values to strings (headers, query, tags). Lists are joined with ", "."""
    if not isinstance(value, Mapping):
        return None
    out: Dict[str, str] = {}
    try:
        for k, v in list(value.items()):
            if v is None:
                continue
            key = k if isinstance(k, str) else _stringify(k)
            if isinstance(v, (list, tuple)):
                out[key] = ", ".join(_stringify(x) for x in v)
            else:
                out[key] = _stringify(v)
    except Exception:
        return out
    return out
