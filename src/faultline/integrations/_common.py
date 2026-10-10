"""Helpers shared by the framework integrations (WSGI, ASGI, Flask, Django, FastAPI)."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qsl

from ..application import CaptureHint, Hub
from ..domain import RequestData, Scope

_CAPTURED_ATTR = "__faultline_captured__"


def query_dict(query_string: str) -> Dict[str, str]:
    grouped: Dict[str, List[str]] = {}
    try:
        for k, v in parse_qsl(query_string, keep_blank_values=True):
            grouped.setdefault(k, []).append(v)
    except Exception:
        return {}
    return {k: ", ".join(v) for k, v in grouped.items()}


def merge_header(headers: Dict[str, str], name: str, value: str) -> None:
    headers[name] = f"{headers[name]}, {value}" if name in headers else value


def request_from_wsgi(environ: Mapping[str, Any]) -> RequestData:
    req: RequestData = {}
    method = environ.get("REQUEST_METHOD")
    if isinstance(method, str):
        req["method"] = method
    headers: Dict[str, str] = {}
    for key, value in environ.items():
        if not isinstance(key, str) or not isinstance(value, str):
            continue
        if key.startswith("HTTP_"):
            headers[key[5:].replace("_", "-").title()] = value
        elif key in ("CONTENT_TYPE", "CONTENT_LENGTH") and value:
            headers[key.replace("_", "-").title()] = value
    if headers:
        req["headers"] = headers
    scheme = environ.get("wsgi.url_scheme") or "http"
    host = headers.get("Host") or environ.get("SERVER_NAME") or ""
    if host and "Host" not in headers:
        port = str(environ.get("SERVER_PORT") or "")
        if port and port not in ("80", "443"):
            host = f"{host}:{port}"
    path = f"{environ.get('SCRIPT_NAME', '')}{environ.get('PATH_INFO', '')}" or "/"
    qs = environ.get("QUERY_STRING") or ""
    req["url"] = f"{scheme}://{host}{path}" + (f"?{qs}" if qs else "") if host else path
    if qs:
        q = query_dict(str(qs))
        if q:
            req["query"] = q
    return req


def request_from_asgi(scope: Mapping[str, Any]) -> RequestData:
    req: RequestData = {}
    method = scope.get("method")
    req["method"] = method if isinstance(method, str) else "GET"
    headers: Dict[str, str] = {}
    raw_headers: Iterable[Tuple[bytes, bytes]] = scope.get("headers") or ()
    for k, v in raw_headers:
        try:
            merge_header(headers, k.decode("latin-1").title(), v.decode("latin-1"))
        except Exception:
            continue
    if headers:
        req["headers"] = headers
    scheme = scope.get("scheme") or "http"
    host = headers.get("Host")
    if not host:
        server = scope.get("server")
        if server and server[0]:
            host = str(server[0])
            port = server[1] if len(server) > 1 else None
            if port and port not in (80, 443):
                host = f"{host}:{port}"
    path = f"{scope.get('root_path', '')}{scope.get('path', '')}" or "/"
    qs_raw = scope.get("query_string") or b""
    qs = qs_raw.decode("latin-1") if isinstance(qs_raw, (bytes, bytearray)) else str(qs_raw)
    req["url"] = (f"{scheme}://{host}{path}" if host else path) + (f"?{qs}" if qs else "")
    if qs:
        q = query_dict(qs)
        if q:
            req["query"] = q
    return req


def apply_request(
    hub: Hub,
    scope: Scope,
    request: RequestData,
    client_ip: Optional[str] = None,
    breadcrumb: bool = True,
) -> None:
    """Attach request data (and, with ``send_default_pii``, the client IP) to ``scope``."""
    try:
        scope.set_request(request)
        client = hub.client
        if client is not None and client.options.send_default_pii and client_ip:
            user = dict(scope.user or {})
            user["ipAddress"] = client_ip
            scope.set_user(user)  # type: ignore[arg-type]
        if breadcrumb:
            url = request.get("url", "/")
            method = request.get("method", "GET")
            hub.add_breadcrumb(
                {
                    "type": "http",
                    "category": "request",
                    "message": f"{method} {url}",
                    "data": {"method": method, "url": url},
                },
                scope,
            )
    except Exception as err:
        hub.logger.error("failed to attach request data", err)


def capture_request_exception(hub: Hub, exc: BaseException, mechanism: str) -> Optional[str]:
    """Capture an exception raised while handling a request, at most once per exception."""
    try:
        if getattr(exc, _CAPTURED_ATTR, False):
            return None
        try:
            setattr(exc, _CAPTURED_ATTR, True)
        except Exception:
            pass
        return hub.capture_exception(
            exc, CaptureHint(tags={"mechanism": mechanism, "handled": "no"})
        )
    except Exception as err:
        hub.logger.error(f"{mechanism}: capture failed", err)
        return None
