"""FaultlineClient: builds events, applies scope data, sampling, ignore_errors, dedupe,
scrubbing and before_send, then hands them to the transport. Every public method is
exception-safe."""

from __future__ import annotations

import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple, cast

from ..domain import (
    INGEST_PATH,
    PLATFORM,
    SDK_NAME,
    Event,
    ExceptionPayload,
    ResolvedOptions,
    ScopeData,
    StackFrame,
    User,
    dedupe_key,
    is_level,
    iso_timestamp,
    matches_ignore_errors,
    safe_repr,
    safe_str,
    sanitize_event,
    scrub_event,
    to_json_safe,
    truncate,
)
from ..domain.limits import MAX_EXCEPTION_DEPTH, MAX_FRAMES, MAX_MESSAGE_LENGTH
from .ports import (
    Clock,
    IdGenerator,
    Logger,
    RandomSource,
    RuntimeInfo,
    SourceContextReader,
    StackExtractor,
    Transport,
)

DEDUPE_WINDOW_SECONDS = 1.0


@dataclass(frozen=True)
class CaptureHint:
    """Per-call context passed to ``capture_exception`` / ``capture_message``."""

    tags: Optional[Mapping[str, str]] = None
    extra: Optional[Mapping[str, Any]] = None
    user: Optional[User] = None
    level: Optional[str] = None
    fingerprint: Optional[Sequence[str]] = None
    contexts: Optional[Mapping[str, Mapping[str, Any]]] = None


@dataclass(frozen=True)
class ClientDeps:
    transport: Transport
    stack: StackExtractor
    source: Optional[SourceContextReader]
    clock: Clock
    ids: IdGenerator
    random: RandomSource
    logger: Logger
    runtime: RuntimeInfo
    sdk_version: str


_EMPTY_HINT = CaptureHint()


def is_exception_group(exc: BaseException) -> bool:
    """Duck-typed so it also recognizes the ``exceptiongroup`` backport on Python < 3.11."""
    subs = getattr(exc, "exceptions", None)
    return (
        type(exc).__name__.endswith("ExceptionGroup")
        and isinstance(subs, (tuple, list))
        and all(isinstance(s, BaseException) for s in subs)
    )


def next_in_chain(exc: BaseException) -> Optional[BaseException]:
    """``__cause__`` if set, else ``__context__`` unless suppressed (``raise ... from None``)."""
    if exc.__cause__ is not None:
        return exc.__cause__
    if exc.__context__ is not None and not exc.__suppress_context__:
        return exc.__context__
    return None


def describe_non_exception(value: object) -> str:
    """Human-readable description of a captured value that is not an exception."""
    try:
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            keys = ", ".join(safe_str(k) for k in list(value.keys())[:10]) or "(none)"
            return f"Non-exception mapping captured with keys: {keys}: " + truncate(
                safe_repr(to_json_safe(value, 3, 20), 2048), 1024
            )
        return f"Non-exception value captured: {type(value).__name__}: {safe_repr(value)}"
    except Exception:
        return "Non-exception value captured"


class FaultlineClient:
    def __init__(self, options: ResolvedOptions, deps: ClientDeps) -> None:
        self.options = options
        self._deps = deps
        self._lock = threading.Lock()
        self._last: Optional[Tuple[str, float]] = None
        self._closed = False

    @property
    def logger(self) -> Logger:
        return self._deps.logger

    @property
    def is_enabled(self) -> bool:
        return self.options.enabled and not self._closed

    def is_ingest_url(self, url: str) -> bool:
        """True when ``url`` targets this client's own ingest endpoint (no self-instrumentation)."""
        return url.startswith(self.options.endpoint + INGEST_PATH)

    # -- capture ----------------------------------------------------------------------------

    def capture_exception(
        self,
        exception: object,
        hint: Optional[CaptureHint] = None,
        scope: Optional[ScopeData] = None,
    ) -> Optional[str]:
        try:
            if not self.is_enabled:
                return None
            hint = hint or _EMPTY_HINT
            payload = self._build_exception(exception)
            level = self._level(hint.level, scope, "error")
            event = self._base_event(level)
            event["exception"] = payload
            event["message"] = payload["value"]
            exc = exception if isinstance(exception, BaseException) else None
            return self._process(event, hint, scope, exc)
        except Exception as err:
            self.logger.error("capture_exception failed", err)
            return None

    def capture_message(
        self,
        message: object,
        level: Optional[str] = None,
        hint: Optional[CaptureHint] = None,
        scope: Optional[ScopeData] = None,
    ) -> Optional[str]:
        try:
            if not self.is_enabled:
                return None
            hint = hint or _EMPTY_HINT
            lvl = level if level is not None and is_level(level) else None
            event = self._base_event(lvl or self._level(hint.level, scope, "info"))
            event["message"] = truncate(safe_str(message), MAX_MESSAGE_LENGTH)
            return self._process(event, hint, scope, None)
        except Exception as err:
            self.logger.error("capture_message failed", err)
            return None

    def flush(self, timeout: float = 2.0) -> bool:
        try:
            return self._deps.transport.flush(timeout)
        except Exception as err:
            self.logger.error("flush failed", err)
            return False

    def close(self, timeout: float = 2.0) -> bool:
        try:
            self._closed = True
            return self._deps.transport.close(timeout)
        except Exception as err:
            self.logger.error("close failed", err)
            return False

    # -- internals --------------------------------------------------------------------------

    @staticmethod
    def _level(hint_level: Optional[str], scope: Optional[ScopeData], default: str) -> str:
        if hint_level is not None and is_level(hint_level):
            return hint_level
        if scope is not None and scope.level is not None and is_level(scope.level):
            return scope.level
        return default

    def _now_iso(self) -> str:
        return iso_timestamp(self._deps.clock.now())

    def _base_event(self, level: str) -> Event:
        event: Event = {
            "eventId": self._deps.ids.uuid(),
            "timestamp": self._now_iso(),
            "level": level,
            "platform": PLATFORM,
            "sdk": {"name": SDK_NAME, "version": self._deps.sdk_version},
        }
        o = self.options
        if o.environment:
            event["environment"] = o.environment
        if o.release:
            event["release"] = o.release
        server_name = o.server_name or self._deps.runtime.server_name()
        if server_name:
            event["serverName"] = server_name
        return event

    def _process(
        self,
        event: Event,
        hint: CaptureHint,
        scope: Optional[ScopeData],
        exc: Optional[BaseException],
    ) -> Optional[str]:
        o, log = self.options, self.logger
        if o.sample_rate < 1 and self._deps.random.random() >= o.sample_rate:
            log.debug(f"event {event['eventId']} dropped by sample_rate")
            return None

        self._apply_scope(event, hint, scope)

        if matches_ignore_errors(event, o.ignore_errors, exc):
            log.debug(f"event {event['eventId']} dropped by ignore_errors")
            return None

        key = dedupe_key(event)
        now = self._deps.clock.monotonic()
        with self._lock:
            last = self._last
            if last is not None and last[0] == key and now - last[1] < DEDUPE_WINDOW_SECONDS:
                log.debug(f"event {event['eventId']} dropped as duplicate")
                return None
            self._last = (key, now)

        if "exception" in event and self._deps.source is not None:
            self._add_source_context(event["exception"])

        prepared = sanitize_event(event, self._deps.sdk_version, self._now_iso())
        if prepared is None:
            return None
        prepared = scrub_event(prepared, o.send_default_pii)

        if o.before_send is not None:
            try:
                result: object = o.before_send(prepared)
            except Exception as err:
                log.warn(f"before_send raised; event {prepared['eventId']} dropped", err)
                return None
            if result is None:
                log.debug(f"event {prepared['eventId']} dropped by before_send")
                return None
            after = sanitize_event(result, self._deps.sdk_version, self._now_iso())
            if after is None:
                log.warn("before_send returned an invalid event; dropped")
                return None
            prepared = after

        if self._closed:
            return None
        self._deps.transport.send(prepared)
        return prepared["eventId"]

    def _apply_scope(self, event: Event, hint: CaptureHint, scope: Optional[ScopeData]) -> None:
        tags: Dict[str, str] = {}
        if scope is not None:
            tags.update(scope.tags)
        if hint.tags:
            tags.update(hint.tags)
        if tags:
            event["tags"] = tags

        user: Dict[str, Any] = {}
        if scope is not None and scope.user:
            user.update(scope.user)
        if hint.user:
            user.update(hint.user)
        if user:
            event["user"] = cast(User, user)

        if scope is not None and scope.request:
            event["request"] = scope.request

        contexts: Dict[str, Any] = dict(self._deps.runtime.contexts())
        if scope is not None:
            contexts.update(scope.contexts)
        if hint.contexts:
            contexts.update(hint.contexts)
        event["contexts"] = contexts

        extra: Dict[str, Any] = {}
        if scope is not None:
            extra.update(scope.extra)
        if hint.extra:
            extra.update(hint.extra)
        if extra:
            event["extra"] = extra

        max_crumbs = self.options.max_breadcrumbs
        if scope is not None and scope.breadcrumbs and max_crumbs > 0:
            event["breadcrumbs"] = list(scope.breadcrumbs[-max_crumbs:])

        fingerprint = hint.fingerprint or (scope.fingerprint if scope is not None else None)
        if fingerprint:
            event["fingerprint"] = [safe_str(x) for x in fingerprint]

    def _add_source_context(self, ex: ExceptionPayload) -> None:
        source = self._deps.source
        if source is None:
            return
        current: Optional[ExceptionPayload] = ex
        while current is not None:
            try:
                frames = current["stacktrace"]["frames"]
                current["stacktrace"]["frames"] = source.add_source_context(frames)
            except Exception as err:
                self.logger.debug("source context failed", err)
            current = current.get("cause")

    def _frames(self, exc: Optional[BaseException]) -> List[StackFrame]:
        try:
            if exc is None:
                frames = self._deps.stack.from_current_stack()
            else:
                frames = self._deps.stack.from_exception(exc)
            return frames[-MAX_FRAMES:]
        except Exception as err:
            self.logger.debug("stack extraction failed", err)
            return []

    def _build_exception(self, value: object) -> ExceptionPayload:
        if not isinstance(value, BaseException):
            return {
                "type": "Error",
                "value": truncate(describe_non_exception(value), MAX_MESSAGE_LENGTH),
                "stacktrace": {"frames": self._frames(None)},
            }

        chain: List[ExceptionPayload] = []
        seen: Set[int] = set()
        current: Optional[BaseException] = value
        while current is not None and len(chain) < MAX_EXCEPTION_DEPTH and id(current) not in seen:
            seen.add(id(current))
            chain.append(self._single(current))
            nxt = next_in_chain(current)
            if nxt is None and is_exception_group(current):
                subs = getattr(current, "exceptions", ())
                nxt = subs[0] if subs else None
            current = nxt

        for parent, child in zip(chain, chain[1:]):
            parent["cause"] = child
        return chain[0]

    def _single(self, exc: BaseException) -> ExceptionPayload:
        message = safe_str(exc)
        if is_exception_group(exc):
            subs = getattr(exc, "exceptions", ())
            summary = ", ".join(
                f"{type(s).__name__}({safe_repr(safe_str(s), 200)})" for s in list(subs)[:10]
            )
            if summary:
                message = f"{message} [sub-exceptions: {summary}]"
        return {
            "type": type(exc).__name__ or "Error",
            "value": truncate(message, MAX_MESSAGE_LENGTH),
            "stacktrace": {"frames": self._frames(exc)},
        }
