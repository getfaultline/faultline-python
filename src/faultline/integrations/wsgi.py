"""Generic WSGI middleware: per-request scope, request data, exception capture.

Usage::

    from faultline.integrations.wsgi import FaultlineWsgiMiddleware
    application = FaultlineWsgiMiddleware(application)
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterable, Iterator
from typing import Any, Callable, Optional

from ..application import Hub, get_hub
from ._common import apply_request, capture_request_exception, request_from_wsgi

WsgiApp = Callable[[Any, Any], Iterable[bytes]]


class FaultlineWsgiMiddleware:
    def __init__(
        self, app: WsgiApp, *, capture_exceptions: bool = True, hub: Optional[Hub] = None
    ) -> None:
        self.app = app
        self.capture_exceptions = capture_exceptions
        self._hub = hub

    @property
    def hub(self) -> Hub:
        return self._hub or get_hub()

    def __call__(self, environ: Any, start_response: Any) -> Iterable[bytes]:
        ctx = contextvars.copy_context()
        return ctx.run(self._call, ctx, environ, start_response)

    def _call(self, ctx: contextvars.Context, environ: Any, start_response: Any) -> Iterable[bytes]:
        hub = self.hub
        try:
            scope = hub.fork_scope()
            hub.activate_scope(scope)
            apply_request(hub, scope, request_from_wsgi(environ), environ.get("REMOTE_ADDR"))
        except Exception as err:
            hub.logger.error("wsgi: failed to set up the request scope", err)
        try:
            result = self.app(environ, start_response)
        except Exception as exc:
            if self.capture_exceptions:
                capture_request_exception(hub, exc, "wsgi")
            raise
        return _ScopedIterable(result, ctx, self)


class _ScopedIterable:
    """Iterates the app's response inside the request context, so errors raised while
    streaming are captured with the request's scope."""

    def __init__(
        self, result: Iterable[bytes], ctx: contextvars.Context, mw: FaultlineWsgiMiddleware
    ) -> None:
        self._result = result
        self._ctx = ctx
        self._mw = mw

    def __iter__(self) -> Iterator[bytes]:
        iterator = iter(self._result)
        while True:
            try:
                chunk = self._ctx.run(next, iterator)
            except StopIteration:
                return
            except Exception as exc:
                if self._mw.capture_exceptions:
                    self._ctx.run(capture_request_exception, self._mw.hub, exc, "wsgi")
                raise
            yield chunk

    def close(self) -> None:
        close = getattr(self._result, "close", None)
        if close is not None:
            self._ctx.run(close)
