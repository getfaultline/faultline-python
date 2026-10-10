"""``logging`` integration: log records become breadcrumbs; optionally, ERROR records that
carry ``exc_info`` are captured as events (off by default).

It wraps ``logging.Logger.callHandlers``, so it sees every record that passes the logger's
level check, whatever handlers the app configured, without adding a handler.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Iterable, Optional

from ..application import CaptureHint, Hub
from ..domain import safe_str, truncate

_IGNORED_LOGGERS = ("faultline",)


def level_name(levelno: int) -> str:
    if levelno >= logging.CRITICAL:
        return "fatal"
    if levelno >= logging.ERROR:
        return "error"
    if levelno >= logging.WARNING:
        return "warning"
    if levelno >= logging.INFO:
        return "info"
    return "debug"


class LoggingIntegration:
    name = "logging"

    def __init__(
        self,
        *,
        breadcrumb_level: int = logging.INFO,
        capture_errors: bool = False,
        event_level: int = logging.ERROR,
        ignore_loggers: Iterable[str] = (),
    ) -> None:
        self.breadcrumb_level = breadcrumb_level
        self.capture_errors = capture_errors
        self.event_level = event_level
        self.ignore_loggers = tuple(_IGNORED_LOGGERS) + tuple(ignore_loggers)
        self._hub: Optional[Hub] = None
        self._original: Optional[Callable[[logging.Logger, logging.LogRecord], None]] = None
        self._wrapper: Optional[Callable[[logging.Logger, logging.LogRecord], None]] = None
        self._local = threading.local()

    def setup(self, hub: Hub) -> None:
        self._hub = hub
        original = logging.Logger.callHandlers
        integration = self

        def callHandlers(logger: logging.Logger, record: logging.LogRecord) -> None:
            integration._record(record)
            original(logger, record)

        self._original = original
        self._wrapper = callHandlers
        logging.Logger.callHandlers = callHandlers  # type: ignore[method-assign,assignment]

    def teardown(self) -> None:
        if self._wrapper is not None and logging.Logger.callHandlers is self._wrapper:
            logging.Logger.callHandlers = self._original  # type: ignore[method-assign,assignment]
        self._hub = None
        self._wrapper = None

    def _ignored(self, name: str) -> bool:
        return any(name == n or name.startswith(n + ".") for n in self.ignore_loggers)

    def _record(self, record: logging.LogRecord) -> None:
        hub = self._hub
        if hub is None or getattr(self._local, "busy", False):
            return
        self._local.busy = True
        try:
            if self._ignored(record.name):
                return
            try:
                message = record.getMessage()
            except Exception:
                message = safe_str(record.msg)
            exc = record.exc_info[1] if record.exc_info else None
            if (
                self.capture_errors
                and record.levelno >= self.event_level
                and isinstance(exc, BaseException)
            ):
                hub.capture_exception(
                    exc,
                    CaptureHint(
                        level=level_name(record.levelno),
                        tags={"logger": record.name[:200], "mechanism": "logging"},
                        extra={"log.message": truncate(message, 8192)},
                    ),
                )
                return
            if record.levelno < self.breadcrumb_level:
                return
            data: dict[str, Any] = {"logger": record.name}
            if exc is not None:
                data["exception"] = f"{type(exc).__name__}: {safe_str(exc)}"
            hub.add_breadcrumb(
                {
                    "type": "log",
                    "category": record.name,
                    "level": level_name(record.levelno),
                    "message": truncate(message, 1000),
                    "data": data,
                }
            )
        except Exception:
            pass
        finally:
            self._local.busy = False
