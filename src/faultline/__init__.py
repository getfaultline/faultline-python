"""Faultline Python SDK: public API and composition root.

This is the only module that wires the application, infrastructure and integration layers
together. Every public function is exception-safe: the SDK never raises into the host app
(except for exceptions raised by your own code inside ``with_scope()``).
"""

from __future__ import annotations

import atexit
import sys
import threading
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from typing import Any, Callable, Dict, List, Optional, TypeVar

from ._version import __version__
from .application import CaptureHint, ClientDeps, FaultlineClient, Hub, Integration, get_hub
from .domain import (
    BeforeSend,
    Breadcrumb,
    Event,
    ExceptionPayload,
    IgnorePattern,
    Level,
    RequestData,
    Scope,
    StackFrame,
    User,
    is_level,
    resolve_options,
    safe_str,
)
from .infrastructure import (
    HttpTransport,
    LinecacheSourceReader,
    PythonRuntimeInfo,
    StderrLogger,
    StdRandom,
    SystemClock,
    TracebackStackExtractor,
    UuidGenerator,
)
from .integrations import (
    ExcepthookIntegration,
    LoggingIntegration,
    StdlibHttpIntegration,
    ThreadingIntegration,
)

__all__ = [
    "BeforeSend",
    "Breadcrumb",
    "Event",
    "ExceptionPayload",
    "Level",
    "LoggingIntegration",
    "RequestData",
    "Scope",
    "StackFrame",
    "User",
    "__version__",
    "add_breadcrumb",
    "capture_exception",
    "capture_message",
    "close",
    "flush",
    "get_current_scope",
    "init",
    "is_enabled",
    "set_context",
    "set_extra",
    "set_tag",
    "set_tags",
    "set_user",
    "with_scope",
]

SDK_VERSION = __version__
ATEXIT_TIMEOUT = 2.0

_T = TypeVar("_T")

_INTEGRATION_FACTORIES: Dict[str, Callable[[], Integration]] = {
    "excepthook": ExcepthookIntegration,
    "threading": ThreadingIntegration,
    "logging": LoggingIntegration,
    "http": StdlibHttpIntegration,
}

_init_lock = threading.Lock()
_atexit_registered = False


def _hub() -> Hub:
    return get_hub()


def _guard(fallback: _T, fn: Callable[[], _T]) -> _T:
    try:
        return fn()
    except Exception as err:
        try:
            _hub().logger.error("internal SDK error", err)
        except Exception:
            pass
        return fallback


def _resolve_integrations(settings: Mapping[str, Any], logger: StderrLogger) -> List[Integration]:
    out: List[Integration] = []
    for name, factory in _INTEGRATION_FACTORIES.items():
        setting = settings.get(name, False)
        if setting is True:
            out.append(factory())
        elif setting is False:
            continue
        elif hasattr(setting, "setup") and hasattr(setting, "teardown"):
            out.append(setting)
        else:
            logger.warn(f'integrations["{name}"] must be a bool or an integration instance')
    return out


def _on_exit() -> None:
    try:
        _hub().close(ATEXIT_TIMEOUT)
    except Exception:
        pass


def init(
    api_key: Optional[str] = None,
    *,
    endpoint: Optional[str] = None,
    environment: Optional[str] = None,
    release: Optional[str] = None,
    server_name: Optional[str] = None,
    sample_rate: Optional[float] = None,
    max_breadcrumbs: Optional[int] = None,
    send_default_pii: Optional[bool] = None,
    before_send: Optional[BeforeSend] = None,
    ignore_errors: Optional[Sequence[IgnorePattern]] = None,
    integrations: Optional[Mapping[str, Any]] = None,
    enabled: Optional[bool] = None,
    debug: Optional[bool] = None,
    **unknown: Any,
) -> None:
    """Initialize the SDK. Calling it again replaces the previous client. Never raises.

    ``integrations`` maps ``"excepthook"``, ``"threading"``, ``"logging"`` and ``"http"`` to
    ``True`` / ``False`` or to a configured instance, e.g.
    ``{"logging": LoggingIntegration(capture_errors=True)}``.
    """

    def run() -> None:
        global _atexit_registered
        with _init_lock:
            hub = _hub()
            previous = hub.client
            hub.teardown_integrations()
            hub.bind_client(None)
            if previous is not None:
                # Let the old client deliver what it already queued, without blocking init.
                threading.Thread(
                    target=previous.close,
                    args=(ATEXIT_TIMEOUT,),
                    name="faultline-close",
                    daemon=True,
                ).start()

            raw: Dict[str, Any] = {
                "api_key": api_key,
                "endpoint": endpoint,
                "environment": environment,
                "release": release,
                "server_name": server_name,
                "sample_rate": sample_rate,
                "max_breadcrumbs": max_breadcrumbs,
                "send_default_pii": send_default_pii,
                "before_send": before_send,
                "ignore_errors": ignore_errors,
                "integrations": integrations,
                "enabled": enabled,
                "debug": debug,
            }
            result = resolve_options(raw)
            logger = StderrLogger(result.debug)
            for key in unknown:
                logger.warn(f'unknown option "{key}"; ignored')
            for w in result.warnings:
                logger.warn(f"invalid option: {w}")
            opts = result.options
            if opts is None:
                logger.error("SDK disabled: " + "; ".join(result.errors))
                return
            if not opts.enabled:
                logger.debug("SDK disabled (enabled=False)")
                return

            transport = HttpTransport(opts.endpoint, opts.api_key, logger)
            client = FaultlineClient(
                opts,
                ClientDeps(
                    transport=transport,
                    stack=TracebackStackExtractor(),
                    source=LinecacheSourceReader(),
                    clock=SystemClock(),
                    ids=UuidGenerator(),
                    random=StdRandom(),
                    logger=logger,
                    runtime=PythonRuntimeInfo(),
                    sdk_version=SDK_VERSION,
                ),
            )
            hub.bind_client(client)
            hub.install_integrations(_resolve_integrations(opts.integrations, logger))
            if not _atexit_registered:
                atexit.register(_on_exit)
                _atexit_registered = True
            logger.debug(f"initialized (endpoint {opts.endpoint}, sdk {SDK_VERSION})")

    _guard(None, run)


def _hint(
    tags: Optional[Mapping[str, Any]],
    extra: Optional[Mapping[str, Any]],
    user: Optional[User],
    level: Optional[str],
    fingerprint: Optional[Sequence[str]],
    contexts: Optional[Mapping[str, Mapping[str, Any]]],
) -> CaptureHint:
    return CaptureHint(
        tags={str(k): safe_str(v) for k, v in tags.items()} if isinstance(tags, Mapping) else None,
        extra=extra if isinstance(extra, Mapping) else None,
        user=user if isinstance(user, Mapping) else None,
        level=level if level is not None and is_level(level) else None,
        fingerprint=[safe_str(x) for x in fingerprint]
        if isinstance(fingerprint, (list, tuple))
        else None,
        contexts=contexts if isinstance(contexts, Mapping) else None,
    )


def capture_exception(
    exc: Any = None,
    *,
    tags: Optional[Mapping[str, Any]] = None,
    extra: Optional[Mapping[str, Any]] = None,
    user: Optional[User] = None,
    level: Optional[Level] = None,
    fingerprint: Optional[Sequence[str]] = None,
    contexts: Optional[Mapping[str, Mapping[str, Any]]] = None,
    **unknown: Any,
) -> Optional[str]:
    """Capture an exception and return its event id (``None`` if dropped or disabled).

    ``exc`` defaults to the exception currently being handled (``sys.exc_info()``). It may
    also be an ``exc_info`` tuple or any non-exception value (captured as a synthetic error).
    """

    def run() -> Optional[str]:
        value: Any = exc
        if value is None:
            value = sys.exc_info()[1]
            if value is None:
                _hub().logger.debug("capture_exception called without an active exception")
                return None
        elif isinstance(value, tuple) and len(value) == 3:
            value = value[1]
        hint = _hint(tags, extra, user, level, fingerprint, contexts)
        return _hub().capture_exception(value, hint)

    return _guard(None, run)


def capture_message(
    message: str,
    level: Level = "info",
    *,
    tags: Optional[Mapping[str, Any]] = None,
    extra: Optional[Mapping[str, Any]] = None,
    user: Optional[User] = None,
    fingerprint: Optional[Sequence[str]] = None,
    contexts: Optional[Mapping[str, Mapping[str, Any]]] = None,
    **unknown: Any,
) -> Optional[str]:
    """Capture a plain message (default level ``"info"``) and return its event id."""

    def run() -> Optional[str]:
        lvl = level if is_level(level) else "info"
        hint = _hint(tags, extra, user, lvl, fingerprint, contexts)
        return _hub().capture_message(message, lvl, hint)

    return _guard(None, run)


def set_user(user: Optional[User]) -> None:
    """Set (or, with ``None``, clear) the user on the current scope."""
    _guard(None, lambda: _hub().set_user(user if isinstance(user, Mapping) and user else None))


def set_tag(key: str, value: Any) -> None:
    def run() -> None:
        if isinstance(key, str):
            _hub().set_tag(key, safe_str(value))

    _guard(None, run)


def set_tags(tags: Mapping[str, Any]) -> None:
    def run() -> None:
        if isinstance(tags, Mapping):
            _hub().set_tags({str(k): safe_str(v) for k, v in tags.items()})

    _guard(None, run)


def set_context(name: str, context: Optional[Mapping[str, Any]]) -> None:
    """Set a named structured context; ``None`` removes it."""

    def run() -> None:
        if isinstance(name, str):
            _hub().set_context(name, context if isinstance(context, Mapping) else None)

    _guard(None, run)


def set_extra(key: str, value: Any) -> None:
    def run() -> None:
        if isinstance(key, str):
            _hub().set_extra(key, value)

    _guard(None, run)


def add_breadcrumb(crumb: Optional[Mapping[str, Any]] = None, **fields: Any) -> None:
    """Add a breadcrumb to the current scope.

    Pass a mapping or keyword arguments: ``type`` (``default``, ``http``, ``log``, ``query``,
    ``navigation``), ``category``, ``message``, ``level`` and ``data``.
    """

    def run() -> None:
        merged: Dict[str, Any] = dict(crumb) if isinstance(crumb, Mapping) else {}
        merged.update(fields)
        _hub().add_breadcrumb(merged)

    _guard(None, run)


@contextmanager
def with_scope() -> Iterator[Scope]:
    """Fork the current scope for the duration of the ``with`` block.

    Changes made to the yielded scope (or through ``set_tag`` and friends) apply only inside
    the block, including threads started with ``contextvars.copy_context()`` and asyncio tasks
    created inside it. Exceptions raised by your code propagate unchanged.
    """
    try:
        cm = _hub().with_scope()
        scope = cm.__enter__()
    except Exception:
        yield Scope()  # detached scope: the SDK must never break the caller
        return
    try:
        yield scope
    except BaseException:
        if not cm.__exit__(*sys.exc_info()):
            raise
    else:
        try:
            cm.__exit__(None, None, None)
        except Exception:
            pass


def get_current_scope() -> Scope:
    """The scope that ``set_tag`` & co. write to right now."""
    return _guard(Scope(), lambda: _hub().get_scope())


def _timeout(value: Any, default: float = 2.0) -> float:
    """Invalid timeouts (``None``, strings, NaN, negative) fall back to the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return default
    if value != value or value < 0:
        return default
    return float(min(value, 3600.0))


def flush(timeout: float = 2.0) -> bool:
    """Block until queued events are sent. Returns ``False`` on timeout. Never raises."""
    return _guard(False, lambda: _hub().flush(_timeout(timeout)))


def close(timeout: float = 2.0) -> bool:
    """Flush, remove the integrations and disable the SDK. Never raises."""
    return _guard(False, lambda: _hub().close(_timeout(timeout)))


def is_enabled() -> bool:
    """``True`` once ``init`` succeeded and the SDK is sending events."""
    return _guard(False, lambda: bool(_hub().client and _hub().client.is_enabled))  # type: ignore[union-attr]
