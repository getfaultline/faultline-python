"""Hub: holds the current client and the scope stack.

The root scope is global. ``with_scope()`` and ``request_scope()`` fork the current scope into
a ``ContextVar``-backed child, so concurrent threads, asyncio tasks and requests each see their
own data. ``contextvars`` is a pure language primitive (no I/O), so it is used directly here
instead of behind a port (ARCHITECTURE.md ADR-003).
"""

from __future__ import annotations

import contextvars
import threading
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any, Callable, List, Optional, Protocol

from ..domain import (
    Breadcrumb,
    Scope,
    User,
    is_breadcrumb_type,
    is_level,
    iso_timestamp,
    safe_str,
)
from ..domain.limits import MAX_BREADCRUMBS
from .client import CaptureHint, FaultlineClient
from .ports import Logger, SilentLogger

_SILENT = SilentLogger()


class Integration(Protocol):
    """An integration hooks into the host runtime and reports through the Hub."""

    name: str

    def setup(self, hub: Hub) -> None: ...

    def teardown(self) -> None: ...


class Hub:
    def __init__(self, clock_now: Optional[Callable[[], float]] = None) -> None:
        self._now: Callable[[], float] = clock_now or time.time
        self._client: Optional[FaultlineClient] = None
        self._root = Scope()
        self._current: contextvars.ContextVar[Optional[Scope]] = contextvars.ContextVar(
            f"faultline_scope_{id(self)}", default=None
        )
        self._integrations: List[Integration] = []
        self._lock = threading.RLock()

    # -- client -----------------------------------------------------------------------------

    @property
    def client(self) -> Optional[FaultlineClient]:
        return self._client

    @property
    def logger(self) -> Logger:
        client = self._client
        return client.logger if client is not None else _SILENT

    def bind_client(self, client: Optional[FaultlineClient]) -> None:
        self._client = client

    # -- scopes -----------------------------------------------------------------------------

    def get_scope(self) -> Scope:
        """The scope active in the current context (or the global root scope)."""
        return self._current.get() or self._root

    @property
    def root_scope(self) -> Scope:
        return self._root

    @contextmanager
    def use_scope(self, scope: Scope) -> Iterator[Scope]:
        """Make ``scope`` current for the duration of the block (context-local)."""
        token = self._current.set(scope)
        try:
            yield scope
        finally:
            try:
                self._current.reset(token)
            except ValueError:
                # Exited in a different context than it was entered in (for example a
                # generator resumed elsewhere). Restore the previous value as best we can.
                self._current.set(token.old_value if token.old_value is not token.MISSING else None)

    @contextmanager
    def with_scope(self) -> Iterator[Scope]:
        """Fork the current scope; changes apply only inside the block."""
        with self.use_scope(self.get_scope().clone()) as scope:
            yield scope

    def activate_scope(self, scope: Scope) -> None:
        """Make ``scope`` current in the running context without restoring it later.

        Only for code that runs inside its own ``contextvars.copy_context()`` (the WSGI
        middleware), where the whole context is discarded afterwards.
        """
        self._current.set(scope)

    def fork_scope(self) -> Scope:
        return self.get_scope().clone()

    # -- capture ----------------------------------------------------------------------------

    def capture_exception(
        self, exception: object, hint: Optional[CaptureHint] = None
    ) -> Optional[str]:
        client = self._client
        if client is None:
            return None
        return client.capture_exception(exception, hint, self.get_scope().get_data())

    def capture_message(
        self, message: object, level: Optional[str] = None, hint: Optional[CaptureHint] = None
    ) -> Optional[str]:
        client = self._client
        if client is None:
            return None
        return client.capture_message(message, level, hint, self.get_scope().get_data())

    def add_breadcrumb(self, crumb: Mapping[str, Any], scope: Optional[Scope] = None) -> None:
        """Add a breadcrumb to ``scope`` (default: the current scope)."""
        ts = crumb.get("timestamp")
        t = crumb.get("type")
        out: Breadcrumb = {
            "timestamp": ts if isinstance(ts, str) else iso_timestamp(self._now()),
            "type": t if isinstance(t, str) and is_breadcrumb_type(t) else "default",
        }
        category = crumb.get("category")
        if category is not None:
            out["category"] = safe_str(category)
        message = crumb.get("message")
        if message is not None:
            out["message"] = safe_str(message)
        level = crumb.get("level")
        if isinstance(level, str) and is_level(level):
            out["level"] = level
        data = crumb.get("data")
        if isinstance(data, Mapping):
            out["data"] = dict(data)
        client = self._client
        max_crumbs = client.options.max_breadcrumbs if client is not None else MAX_BREADCRUMBS
        (scope or self.get_scope()).add_breadcrumb(out, max_crumbs)

    def set_user(self, user: Optional[User]) -> None:
        self.get_scope().set_user(user)

    def set_tag(self, key: str, value: str) -> None:
        self.get_scope().set_tag(key, value)

    def set_tags(self, tags: Mapping[str, str]) -> None:
        self.get_scope().set_tags(tags)

    def set_context(self, name: str, context: Optional[Mapping[str, Any]]) -> None:
        self.get_scope().set_context(name, context)

    def set_extra(self, key: str, value: Any) -> None:
        self.get_scope().set_extra(key, value)

    # -- integrations -----------------------------------------------------------------------

    def install_integrations(self, integrations: List[Integration]) -> None:
        for integration in integrations:
            try:
                integration.setup(self)
                with self._lock:
                    self._integrations.append(integration)
                self.logger.debug(f'integration "{integration.name}" installed')
            except Exception as err:
                self.logger.error(f'integration "{integration.name}" failed to install', err)

    def teardown_integrations(self) -> None:
        with self._lock:
            integrations, self._integrations = self._integrations, []
        for integration in reversed(integrations):
            try:
                integration.teardown()
            except Exception as err:
                self.logger.error(f'integration "{integration.name}" failed to tear down', err)

    # -- lifecycle --------------------------------------------------------------------------

    def flush(self, timeout: float = 2.0) -> bool:
        client = self._client
        return client.flush(timeout) if client is not None else True

    def close(self, timeout: float = 2.0) -> bool:
        self.teardown_integrations()
        client = self._client
        if client is None:
            return True
        ok = client.close(timeout)
        if self._client is client:
            self._client = None
        return ok


_HUB = Hub()


def get_hub() -> Hub:
    """The process-wide hub. Integrations use it; the public API in ``faultline`` wraps it."""
    return _HUB
