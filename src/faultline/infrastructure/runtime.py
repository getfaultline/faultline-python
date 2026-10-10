"""Small runtime adapters: logger, clock, ids, randomness, runtime/OS info."""

from __future__ import annotations

import platform
import random
import socket
import sys
import time
import uuid
from typing import Any, Dict, Optional, TextIO


class StderrLogger:
    """Writes ``[faultline] ...`` lines to stderr, only when ``debug`` is on.

    It deliberately bypasses the ``logging`` module so the logging integration can never
    record (or loop on) the SDK's own diagnostics.
    """

    def __init__(self, enabled: bool, stream: Optional[TextIO] = None) -> None:
        self.enabled = enabled
        self._stream = stream

    def _write(self, level: str, message: str, args: Any) -> None:
        if not self.enabled:
            return
        try:
            stream = self._stream or sys.stderr
            extra = " ".join(_fmt(a) for a in args)
            stream.write(f"[faultline] {level}: {message}{' ' + extra if extra else ''}\n")
        except Exception:
            pass

    def debug(self, message: str, *args: Any) -> None:
        self._write("debug", message, args)

    def warn(self, message: str, *args: Any) -> None:
        self._write("warning", message, args)

    def error(self, message: str, *args: Any) -> None:
        self._write("error", message, args)


def _fmt(arg: Any) -> str:
    try:
        if isinstance(arg, BaseException):
            return f"{type(arg).__name__}: {arg}"
        return repr(arg)
    except Exception:
        return "<unrepresentable>"


class SystemClock:
    def now(self) -> float:
        return time.time()

    def monotonic(self) -> float:
        return time.monotonic()


class UuidGenerator:
    def uuid(self) -> str:
        return str(uuid.uuid4())


class StdRandom:
    def random(self) -> float:
        return random.random()


class PythonRuntimeInfo:
    def __init__(self) -> None:
        self._contexts: Dict[str, Any] = {
            "runtime": {
                "name": "python",
                "version": _safe(platform.python_version, "unknown"),
            },
            "os": {"name": _safe(platform.system, "unknown") or "unknown"},
        }
        release = _safe(platform.release, "")
        if release:
            self._contexts["os"]["version"] = release
        self._server_name = _safe(socket.gethostname, "") or None

    def contexts(self) -> Dict[str, Any]:
        return {k: dict(v) for k, v in self._contexts.items()}

    def server_name(self) -> Optional[str]:
        return self._server_name


def _safe(fn: Any, fallback: str) -> str:
    try:
        value = fn()
        return value if isinstance(value, str) else fallback
    except Exception:
        return fallback
