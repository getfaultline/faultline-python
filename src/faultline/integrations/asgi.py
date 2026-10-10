"""Generic ASGI middleware (Starlette, FastAPI, Quart, Django ASGI, ...).

Usage::

    from faultline.integrations.asgi import FaultlineAsgiMiddleware
    app = FaultlineAsgiMiddleware(app)
    # or, with Starlette / FastAPI: app.add_middleware(FaultlineAsgiMiddleware)
"""

from __future__ import annotations

from collections.abc import Awaitable, MutableMapping
from typing import Any, Callable, Optional

from ..application import Hub, get_hub
from ._common import apply_request, capture_request_exception, request_from_asgi

AsgiScope = MutableMapping[str, Any]
AsgiReceive = Callable[[], Awaitable[MutableMapping[str, Any]]]
AsgiSend = Callable[[MutableMapping[str, Any]], Awaitable[None]]
AsgiApp = Callable[[AsgiScope, AsgiReceive, AsgiSend], Awaitable[None]]


class FaultlineAsgiMiddleware:
    def __init__(
        self, app: AsgiApp, *, capture_exceptions: bool = True, hub: Optional[Hub] = None
    ) -> None:
        self.app = app
        self.capture_exceptions = capture_exceptions
        self._hub = hub

    @property
    def hub(self) -> Hub:
        return self._hub or get_hub()

    async def __call__(self, scope: AsgiScope, receive: AsgiReceive, send: AsgiSend) -> None:
        if scope.get("type") not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        hub = self.hub
        with hub.with_scope() as fl_scope:
            try:
                client = scope.get("client")
                ip = str(client[0]) if client else None
                apply_request(hub, fl_scope, request_from_asgi(scope), ip)
            except Exception as err:
                hub.logger.error("asgi: failed to set up the request scope", err)
            try:
                await self.app(scope, receive, send)
            except Exception as exc:
                if self.capture_exceptions:
                    capture_request_exception(hub, exc, "asgi")
                raise
