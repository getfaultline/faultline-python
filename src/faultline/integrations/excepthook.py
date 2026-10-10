"""Uncaught exceptions: ``sys.excepthook`` and ``threading.excepthook``.

Both hooks capture, then call the previous hook, so the default behavior (printing the
traceback, the exit code) is unchanged. The ``sys`` hook also flushes for up to 2 s, because
the interpreter is about to exit.
"""

from __future__ import annotations

import sys
import threading
from types import TracebackType
from typing import Any, Callable, Optional, Type

from ..application import CaptureHint, Hub

FLUSH_TIMEOUT = 2.0


def _capture(hub: Hub, exc: BaseException, level: str, mechanism: str) -> None:
    try:
        hub.capture_exception(
            exc, CaptureHint(level=level, tags={"mechanism": mechanism, "handled": "no"})
        )
    except Exception as err:
        hub.logger.error(f"{mechanism} capture failed", err)


class ExcepthookIntegration:
    name = "excepthook"

    def __init__(self) -> None:
        self._previous: Optional[Callable[..., Any]] = None
        self._installed: Optional[Callable[..., Any]] = None

    def setup(self, hub: Hub) -> None:
        previous = sys.excepthook

        def faultline_excepthook(
            exc_type: Type[BaseException],
            exc: BaseException,
            tb: Optional[TracebackType],
        ) -> None:
            try:
                if not isinstance(exc, KeyboardInterrupt):
                    _capture(hub, exc, "fatal", "excepthook")
                    hub.flush(FLUSH_TIMEOUT)
            finally:
                previous(exc_type, exc, tb)

        self._previous = previous
        self._installed = faultline_excepthook
        sys.excepthook = faultline_excepthook

    def teardown(self) -> None:
        if self._installed is not None and sys.excepthook is self._installed:
            sys.excepthook = self._previous or sys.__excepthook__
        self._installed = None


class ThreadingIntegration:
    name = "threading"

    def __init__(self) -> None:
        self._previous: Optional[Callable[..., Any]] = None
        self._installed: Optional[Callable[..., Any]] = None

    def setup(self, hub: Hub) -> None:
        previous = threading.excepthook

        def faultline_thread_excepthook(args: Any) -> None:
            try:
                exc = args.exc_value
                if exc is not None and not isinstance(exc, SystemExit):
                    _capture(hub, exc, "error", "threading")
            finally:
                previous(args)

        self._previous = previous
        self._installed = faultline_thread_excepthook
        threading.excepthook = faultline_thread_excepthook

    def teardown(self) -> None:
        if self._installed is not None and threading.excepthook is self._installed:
            threading.excepthook = self._previous or threading.__excepthook__
        self._installed = None
