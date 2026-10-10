"""Outgoing ``http.client`` requests become ``http`` breadcrumbs.

``urllib.request`` and most third-party clients (``urllib3``/``requests``) are built on
``http.client.HTTPConnection``, so they are covered too. The instrumentation is passive: it
never changes arguments, return values or exceptions. The SDK's own ingest requests are
skipped.
"""

from __future__ import annotations

import http.client
import threading
from typing import Any, Callable, Optional, Tuple

from ..application import Hub
from ..application.ports import TRANSPORT_THREAD_NAME
from ..domain import Scope

_ATTR = "_faultline_request"


def _level_for(status: Optional[int]) -> str:
    if status is None or status >= 500:
        return "error"
    if status >= 400:
        return "warning"
    return "info"


def _full_url(conn: http.client.HTTPConnection, url: str) -> str:
    if url.startswith(("http://", "https://")):
        return url  # absolute form (proxies)
    https = isinstance(conn, http.client.HTTPSConnection)
    scheme = "https" if https else "http"
    host = conn.host
    port = conn.port
    default = 443 if https else 80
    netloc = host if port in (None, default) else f"{host}:{port}"
    if ":" in host and not host.startswith("["):
        netloc = f"[{host}]" if port in (None, default) else f"[{host}]:{port}"
    return f"{scheme}://{netloc}{url if url.startswith('/') else '/' + url}"


class StdlibHttpIntegration:
    name = "http"

    def __init__(self) -> None:
        self._originals: Optional[Tuple[Callable[..., Any], Callable[..., Any]]] = None
        self._patched: Optional[Tuple[Callable[..., Any], Callable[..., Any]]] = None
        self._hub: Optional[Hub] = None

    def setup(self, hub: Hub) -> None:
        self._hub = hub
        orig_put = http.client.HTTPConnection.putrequest
        orig_get = http.client.HTTPConnection.getresponse
        integration = self

        def putrequest(
            self: http.client.HTTPConnection, method: str, url: str, *a: Any, **kw: Any
        ) -> Any:
            try:
                info = integration._start(self, method, url)
                setattr(self, _ATTR, info)
            except Exception:
                pass
            return orig_put(self, method, url, *a, **kw)

        def getresponse(self: http.client.HTTPConnection, *a: Any, **kw: Any) -> Any:
            info = getattr(self, _ATTR, None)
            if info is not None:
                try:
                    setattr(self, _ATTR, None)
                except Exception:
                    pass
            try:
                response = orig_get(self, *a, **kw)
            except BaseException as err:
                integration._finish(info, None, err)
                raise
            integration._finish(info, getattr(response, "status", None), None)
            return response

        self._originals = (orig_put, orig_get)
        self._patched = (putrequest, getresponse)
        http.client.HTTPConnection.putrequest = putrequest  # type: ignore[method-assign]
        http.client.HTTPConnection.getresponse = getresponse  # type: ignore[method-assign]

    def teardown(self) -> None:
        if self._originals is None or self._patched is None:
            return
        if http.client.HTTPConnection.putrequest is self._patched[0]:
            http.client.HTTPConnection.putrequest = self._originals[0]  # type: ignore[method-assign]
        if http.client.HTTPConnection.getresponse is self._patched[1]:
            http.client.HTTPConnection.getresponse = self._originals[1]  # type: ignore[method-assign]
        self._originals = self._patched = None
        self._hub = None

    def _start(
        self, conn: http.client.HTTPConnection, method: str, url: str
    ) -> Optional[Tuple[str, str, Scope]]:
        hub = self._hub
        if hub is None or threading.current_thread().name == TRANSPORT_THREAD_NAME:
            return None
        full = _full_url(conn, url)
        client = hub.client
        if client is not None and client.is_ingest_url(full):
            return None
        # Capture the scope now: the response may be read in another context.
        return (str(method), full, hub.get_scope())

    def _finish(
        self,
        info: Optional[Tuple[str, str, Scope]],
        status: Optional[int],
        error: Optional[BaseException],
    ) -> None:
        hub = self._hub
        if hub is None or info is None:
            return
        try:
            method, url, scope = info
            data: dict[str, Any] = {"method": method, "url": url}
            if status is not None:
                data["status_code"] = status
            if error is not None:
                data["error"] = f"{type(error).__name__}: {error}"
            hub.add_breadcrumb(
                {
                    "type": "http",
                    "category": "http.client",
                    "level": _level_for(status),
                    "data": data,
                },
                scope,
            )
        except Exception:
            pass
