"""SDK options and their validation.

Hand-written validator (zero runtime dependencies). It never raises: an invalid optional value
falls back to its default with a warning, and only fatal problems (missing ``api_key``,
unusable ``endpoint``) disable the SDK.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple, Type, Union

from .event import Event
from .limits import MAX_BREADCRUMBS

DEFAULT_ENDPOINT = "https://api.faultsline.dev"
INGEST_PATH = "/api/v1/ingest/events"

#: Built-in integrations and whether they are on by default.
DEFAULT_INTEGRATIONS: Mapping[str, bool] = {
    "excepthook": True,
    "threading": True,
    "logging": True,
    "http": True,
}

BeforeSend = Callable[[Event], Optional[Event]]
IgnorePattern = Union[str, "re.Pattern[str]", Type[BaseException]]

_ENDPOINT_RE = re.compile(r"^https?://[^/\s?#]+(/[^\s?#]*)?$", re.IGNORECASE)


@dataclass(frozen=True)
class ResolvedOptions:
    api_key: str = ""
    endpoint: str = DEFAULT_ENDPOINT
    environment: Optional[str] = None
    release: Optional[str] = None
    server_name: Optional[str] = None
    sample_rate: float = 1.0
    max_breadcrumbs: int = 50
    send_default_pii: bool = False
    before_send: Optional[BeforeSend] = None
    ignore_errors: Tuple[IgnorePattern, ...] = ()
    #: name -> True / False / a configured integration object (resolved by the composition root).
    integrations: Mapping[str, Any] = field(default_factory=lambda: dict(DEFAULT_INTEGRATIONS))
    enabled: bool = True
    debug: bool = False


@dataclass(frozen=True)
class OptionsResult:
    options: Optional[ResolvedOptions]
    errors: List[str]
    warnings: List[str]
    debug: bool

    @property
    def ok(self) -> bool:
        return self.options is not None


def parse_endpoint(value: object) -> Optional[str]:
    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not _ENDPOINT_RE.match(candidate):
        return None
    return candidate.rstrip("/")


def resolve_options(raw: Mapping[str, Any]) -> OptionsResult:
    warnings: List[str] = []
    errors: List[str] = []
    debug = raw.get("debug") is True

    def opt_bool(key: str, default: bool) -> bool:
        v = raw.get(key)
        if v is None:
            return default
        if not isinstance(v, bool):
            warnings.append(f'"{key}" must be a bool; using {default}')
            return default
        return v

    def opt_str(key: str, max_length: int = 200) -> Optional[str]:
        v = raw.get(key)
        if v is None:
            return None
        if not isinstance(v, str) or not v.strip():
            warnings.append(f'"{key}" must be a non-empty string; ignored')
            return None
        return v[:max_length]

    enabled = opt_bool("enabled", True)

    api_key = ""
    v = raw.get("api_key")
    if isinstance(v, str) and v.strip():
        api_key = v.strip()
    elif enabled:
        errors.append('"api_key" is required')

    endpoint = DEFAULT_ENDPOINT
    if raw.get("endpoint") is not None:
        parsed = parse_endpoint(raw.get("endpoint"))
        if parsed:
            endpoint = parsed
        else:
            errors.append('"endpoint" must be an absolute http(s) URL')

    sample_rate = 1.0
    v = raw.get("sample_rate")
    if v is not None:
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 1:
            sample_rate = float(v)
        else:
            warnings.append('"sample_rate" must be a number between 0 and 1; using 1.0')

    max_breadcrumbs = 50
    v = raw.get("max_breadcrumbs")
    if v is not None:
        if isinstance(v, int) and not isinstance(v, bool) and v >= 0:
            max_breadcrumbs = min(v, MAX_BREADCRUMBS)
            if v > MAX_BREADCRUMBS:
                warnings.append(f'"max_breadcrumbs" capped at {MAX_BREADCRUMBS}')
        else:
            warnings.append('"max_breadcrumbs" must be a non-negative int; using 50')

    before_send: Optional[BeforeSend] = None
    v = raw.get("before_send")
    if v is not None:
        if callable(v):
            before_send = v
        else:
            warnings.append('"before_send" must be callable; ignored')

    ignore_errors: List[IgnorePattern] = []
    v = raw.get("ignore_errors")
    if v is not None:
        if isinstance(v, (list, tuple, set, frozenset)):
            for item in v:
                if isinstance(item, (str, re.Pattern)) or (
                    isinstance(item, type) and issubclass(item, BaseException)
                ):
                    ignore_errors.append(item)
                else:
                    warnings.append(
                        '"ignore_errors" entries must be str, re.Pattern or exception classes;'
                        " entry ignored"
                    )
        else:
            warnings.append('"ignore_errors" must be a list; ignored')

    integrations: Dict[str, Any] = dict(DEFAULT_INTEGRATIONS)
    v = raw.get("integrations")
    if v is not None:
        if isinstance(v, Mapping):
            for name, setting in v.items():
                if name not in DEFAULT_INTEGRATIONS:
                    warnings.append(f'unknown integration "{name}"; ignored')
                elif setting is None:
                    continue
                else:
                    integrations[name] = setting
        else:
            warnings.append('"integrations" must be a dict; using defaults')

    if errors:
        return OptionsResult(options=None, errors=errors, warnings=warnings, debug=debug)

    return OptionsResult(
        options=ResolvedOptions(
            api_key=api_key,
            endpoint=endpoint,
            environment=opt_str("environment"),
            release=opt_str("release"),
            server_name=opt_str("server_name"),
            sample_rate=sample_rate,
            max_breadcrumbs=max_breadcrumbs,
            send_default_pii=opt_bool("send_default_pii", False),
            before_send=before_send,
            ignore_errors=tuple(ignore_errors),
            integrations=integrations,
            enabled=enabled,
            debug=debug,
        ),
        errors=errors,
        warnings=warnings,
        debug=debug,
    )
