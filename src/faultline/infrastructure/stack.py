"""Traceback to contract frames, ordered oldest to newest.

Python tracebacks are already oldest-first (``walk_tb`` yields the outermost frame first), so
no reordering is needed for exceptions. ``from_current_stack`` walks ``f_back`` (newest-first)
and reverses it.
"""

from __future__ import annotations

import itertools
import linecache
import sys
import sysconfig
from types import CodeType, FrameType, TracebackType
from typing import Iterable, List, Optional, Sequence, Tuple

from ..domain import StackFrame

_SDK_PACKAGE = "faultline"


def _norm(path: str) -> str:
    p = path.replace("\\", "/")
    # Windows drive paths compare case-insensitively.
    if len(p) > 1 and p[1] == ":":
        p = p.lower()
    return p


def default_stdlib_paths() -> Tuple[str, ...]:
    paths = set()
    try:
        cfg = sysconfig.get_paths()
        for key in ("stdlib", "platstdlib"):
            if cfg.get(key):
                paths.add(cfg[key])
    except Exception:
        pass
    for prefix in {sys.base_prefix, sys.base_exec_prefix}:
        if prefix:
            ver = f"python{sys.version_info[0]}.{sys.version_info[1]}"
            paths.add(f"{prefix}/lib/{ver}")
            paths.add(f"{prefix}/Lib")  # Windows layout
    return tuple(sorted({_norm(p).rstrip("/") + "/" for p in paths}))


def is_sdk_module(module: Optional[str]) -> bool:
    return bool(module) and (module == _SDK_PACKAGE or str(module).startswith(_SDK_PACKAGE + "."))


def is_in_app(filename: str, module: Optional[str], stdlib_paths: Sequence[str]) -> bool:
    """CONTRACTS.md section 7: Python frames are in-app unless they live under
    ``site-packages`` / ``dist-packages`` or the standard library. Synthetic filenames
    (``<frozen ...>``, ``<string>``) and the SDK's own frames are never in-app."""
    if not filename or filename.startswith("<"):
        return False
    if is_sdk_module(module):
        return False
    norm = _norm(filename)
    if "/site-packages/" in norm or "/dist-packages/" in norm:
        return False
    return not any(norm.startswith(p) for p in stdlib_paths)


def _column(code: CodeType, lasti: int) -> Optional[int]:
    """1-based column of the instruction at ``lasti`` (Python 3.11+), else None."""
    positions = getattr(code, "co_positions", None)
    if positions is None or lasti < 0:
        return None
    try:
        pos = next(itertools.islice(positions(), lasti // 2, None), None)
    except Exception:
        return None
    if pos is None or pos[2] is None:
        return None
    return int(pos[2]) + 1


class TracebackStackExtractor:
    def __init__(self, stdlib_paths: Optional[Sequence[str]] = None) -> None:
        self._stdlib = tuple(stdlib_paths) if stdlib_paths is not None else default_stdlib_paths()

    def from_exception(self, exc: BaseException) -> List[StackFrame]:
        try:
            tb = exc.__traceback__
            return self._build((tb.tb_frame, tb.tb_lineno, tb.tb_lasti) for tb in _walk(tb))
        except Exception:
            return []

    def from_current_stack(self) -> List[StackFrame]:
        try:
            frames: List[Tuple[FrameType, int, int]] = []
            f: Optional[FrameType] = sys._getframe(1)
            while f is not None:
                frames.append((f, f.f_lineno or 0, f.f_lasti))
                f = f.f_back
            frames.reverse()
            built = self._build(frames)
            # Drop the SDK's own frames so the stack ends at the caller.
            while built and is_sdk_module(built[-1].get("module")):
                built.pop()
            return built
        except Exception:
            return []

    def _build(self, items: Iterable[Tuple[FrameType, int, int]]) -> List[StackFrame]:
        out: List[StackFrame] = []
        for frame, lineno, lasti in items:
            try:
                out.append(self._frame(frame, lineno, lasti))
            except Exception:
                continue
        return out

    def _frame(self, frame: FrameType, lineno: int, lasti: int) -> StackFrame:
        code = frame.f_code
        filename = code.co_filename or "<unknown>"
        module = frame.f_globals.get("__name__")
        module = module if isinstance(module, str) else None
        in_app = is_in_app(filename, module, self._stdlib)
        sf: StackFrame = {"filename": filename, "inApp": in_app}
        sf["function"] = getattr(code, "co_qualname", None) or code.co_name
        if module:
            sf["module"] = module
        if lineno:
            sf["lineno"] = lineno
        col = _column(code, lasti)
        if col is not None:
            sf["colno"] = col
        if in_app:
            # Lets linecache find sources served by import loaders (zip apps etc.) later.
            try:
                linecache.lazycache(filename, frame.f_globals)
            except Exception:
                pass
        return sf


def _walk(tb: Optional[TracebackType]) -> Iterable[TracebackType]:
    while tb is not None:
        yield tb
        tb = tb.tb_next
