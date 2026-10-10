"""Source context (+-5 lines) for in-app frames, read through ``linecache``.

``linecache`` is the same cache the ``traceback`` module uses; it also serves sources from
import loaders (zip apps, eggs) thanks to ``linecache.lazycache`` in the stack extractor.
"""

from __future__ import annotations

import linecache
from typing import List

from ..domain import StackFrame, truncate
from ..domain.limits import CONTEXT_LINES, MAX_CONTEXT_LINE_LENGTH

#: Files with more lines than this are skipped (generated or minified sources).
MAX_FILE_LINES = 50_000


def _clip(line: str) -> str:
    return truncate(line.rstrip("\r\n"), MAX_CONTEXT_LINE_LENGTH)


class LinecacheSourceReader:
    def __init__(self, context_lines: int = CONTEXT_LINES) -> None:
        self._n = max(0, min(context_lines, CONTEXT_LINES))

    def add_source_context(self, frames: List[StackFrame]) -> List[StackFrame]:
        try:
            return [self._enrich(f) for f in frames]
        except Exception:
            return frames

    def _enrich(self, frame: StackFrame) -> StackFrame:
        lineno = frame.get("lineno")
        if not frame["inApp"] or lineno is None or lineno < 1:
            return frame
        try:
            lines = linecache.getlines(frame["filename"])
        except Exception:
            return frame
        if not lines or len(lines) > MAX_FILE_LINES or lineno > len(lines):
            return frame
        idx = lineno - 1
        out: StackFrame = dict(frame)  # type: ignore[assignment]
        out["preContext"] = [_clip(x) for x in lines[max(0, idx - self._n) : idx]]
        out["contextLine"] = _clip(lines[idx])
        out["postContext"] = [_clip(x) for x in lines[idx + 1 : idx + 1 + self._n]]
        return out
