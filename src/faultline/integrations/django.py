"""Django integration.

Add the middleware first in ``MIDDLEWARE``::

    MIDDLEWARE = [
        "faultline.integrations.django.FaultlineMiddleware",
        ...
    ]

and call ``faultline.init(...)`` in ``settings.py`` (or your ``AppConfig.ready``). The
middleware forks a scope per request and attaches request data. Unhandled exceptions are
captured through Django's ``got_request_exception`` signal, which fires only for errors that
become a 500 response (not for ``Http404``, ``PermissionDenied``, ``BadRequest`` or
``SuspiciousOperation``). Works under WSGI and ASGI.
"""

from __future__ import annotations

import sys
from typing import Any, Callable, Optional

from ..application import Hub, get_hub
from ..domain import RequestData, Scope
from ._common import apply_request, capture_request_exception

__all__ = ["FaultlineMiddleware", "request_from_django"]

_DISPATCH_UID = "faultline.integrations.django.got_request_exception"


def request_from_django(request: Any) -> RequestData:
    req: RequestData = {}
    try:
        req["method"] = str(request.method)
    except Exception:
        pass
    try:
        req["url"] = request.build_absolute_uri()
    except Exception:  # DisallowedHost and friends
        try:
            req["url"] = request.get_full_path()
        except Exception:
            pass
    try:
        req["headers"] = {str(k): str(v) for k, v in request.headers.items()}
    except Exception:
        pass
    try:
        query = {str(k): ", ".join(str(x) for x in request.GET.getlist(k)) for k in request.GET}
        if query:
            req["query"] = query
    except Exception:
        pass
    return req


def _on_got_request_exception(sender: Any, request: Any = None, **_: Any) -> None:
    exc = sys.exc_info()[1]
    if exc is not None:
        capture_request_exception(get_hub(), exc, "django")


class FaultlineMiddleware:
    sync_capable = True
    async_capable = True

    def __init__(self, get_response: Callable[[Any], Any]) -> None:
        from asgiref.sync import iscoroutinefunction, markcoroutinefunction
        from django.core.signals import got_request_exception

        self.get_response = get_response
        self._async = iscoroutinefunction(get_response)
        if self._async:
            markcoroutinefunction(self)
        got_request_exception.connect(
            _on_got_request_exception, weak=False, dispatch_uid=_DISPATCH_UID
        )

    def __call__(self, request: Any) -> Any:
        if self._async:
            return self.__acall__(request)
        hub = get_hub()
        with hub.with_scope() as scope:
            self._prepare(hub, scope, request)
            return self.get_response(request)

    async def __acall__(self, request: Any) -> Any:
        hub = get_hub()
        with hub.with_scope() as scope:
            self._prepare(hub, scope, request)
            return await self.get_response(request)

    @staticmethod
    def _prepare(hub: Hub, scope: Scope, request: Any) -> None:
        ip: Optional[str] = None
        try:
            meta = request.META
            ip = meta.get("REMOTE_ADDR")
        except Exception:
            pass
        apply_request(hub, scope, request_from_django(request), ip)
