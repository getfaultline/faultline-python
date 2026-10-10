"""Event payload contract (SDK to server).

Mirrors section 1 of the Faultline spec and CONTRACTS.md sections 2 and 7. Keys are the wire
names (camelCase), so a built event serializes with ``json.dumps`` unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, List, Literal, Tuple, TypedDict

Level = Literal["fatal", "error", "warning", "info", "debug"]
BreadcrumbType = Literal["default", "http", "log", "query", "navigation"]

LEVELS: Tuple[str, ...] = ("fatal", "error", "warning", "info", "debug")
BREADCRUMB_TYPES: Tuple[str, ...] = ("default", "http", "log", "query", "navigation")

SDK_NAME = "getfaultline"
PLATFORM = "python"


class _StackFrameRequired(TypedDict):
    filename: str
    inApp: bool


class StackFrame(_StackFrameRequired, total=False):
    function: str
    module: str
    lineno: int
    colno: int
    preContext: List[str]
    contextLine: str
    postContext: List[str]


class Stacktrace(TypedDict):
    frames: List[StackFrame]


class _ExceptionRequired(TypedDict):
    type: str
    value: str
    stacktrace: Stacktrace


class ExceptionPayload(_ExceptionRequired, total=False):
    cause: ExceptionPayload


class User(TypedDict, total=False):
    id: str
    email: str
    username: str
    ipAddress: str


class RequestData(TypedDict, total=False):
    method: str
    url: str
    headers: Dict[str, str]
    query: Dict[str, str]


class _BreadcrumbRequired(TypedDict):
    timestamp: str
    type: str


class Breadcrumb(_BreadcrumbRequired, total=False):
    category: str
    message: str
    level: str
    data: Dict[str, Any]


class SdkInfo(TypedDict):
    name: str
    version: str


class _EventRequired(TypedDict):
    eventId: str
    timestamp: str
    level: str
    platform: str
    sdk: SdkInfo


class Event(_EventRequired, total=False):
    environment: str
    release: str
    serverName: str
    message: str
    exception: ExceptionPayload
    fingerprint: List[str]
    tags: Dict[str, str]
    user: User
    request: RequestData
    contexts: Dict[str, Any]
    breadcrumbs: List[Breadcrumb]
    extra: Dict[str, Any]


def is_level(value: object) -> bool:
    return isinstance(value, str) and value in LEVELS


def is_breadcrumb_type(value: object) -> bool:
    return isinstance(value, str) and value in BREADCRUMB_TYPES
