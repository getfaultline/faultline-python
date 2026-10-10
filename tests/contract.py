"""Pydantic mirror of the event payload contract (build prompt section 1, CONTRACTS.md 2 and 7).

Strict models (``extra="forbid"``): any field the server would not expect fails the test.
Test-only: the SDK itself has zero runtime dependencies.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")

LevelT = Literal["fatal", "error", "warning", "info", "debug"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class StackFrameModel(Strict):
    filename: str = Field(min_length=1)
    function: Optional[str] = None
    module: Optional[str] = None
    lineno: Optional[int] = Field(default=None, ge=0)
    colno: Optional[int] = Field(default=None, ge=0)
    inApp: bool
    preContext: Optional[List[str]] = Field(default=None, max_length=5)
    contextLine: Optional[str] = Field(default=None, max_length=300)
    postContext: Optional[List[str]] = Field(default=None, max_length=5)

    @field_validator("preContext", "postContext")
    @classmethod
    def _line_length(cls, v: Optional[List[str]]) -> Optional[List[str]]:
        assert v is None or all(len(x) <= 300 for x in v)
        return v


class StacktraceModel(Strict):
    frames: List[StackFrameModel] = Field(max_length=100)


class ExceptionModel(Strict):
    type: str = Field(min_length=1)
    value: str = Field(max_length=8192)
    stacktrace: StacktraceModel
    cause: Optional[ExceptionModel] = None

    def depth(self) -> int:
        return 1 + (self.cause.depth() if self.cause else 0)


class UserModel(Strict):
    id: Optional[str] = None
    email: Optional[str] = None
    username: Optional[str] = None
    ipAddress: Optional[str] = None


class RequestModel(Strict):
    method: Optional[str] = None
    url: Optional[str] = None
    headers: Optional[Dict[str, str]] = None
    query: Optional[Dict[str, str]] = None


class BreadcrumbModel(Strict):
    timestamp: str
    type: Literal["default", "http", "log", "query", "navigation"]
    category: Optional[str] = None
    message: Optional[str] = None
    level: Optional[LevelT] = None
    data: Optional[Dict[str, Any]] = None


class RuntimeModel(Strict):
    name: str
    version: str


class OsModel(Strict):
    name: str
    version: Optional[str] = None


class SdkModel(Strict):
    name: Literal["getfaultline"]
    version: str = Field(min_length=1)


class EventModel(Strict):
    eventId: str
    timestamp: str
    level: LevelT
    platform: Literal["python"]
    environment: Optional[str] = None
    release: Optional[str] = None
    serverName: Optional[str] = None
    message: Optional[str] = Field(default=None, max_length=8192)
    exception: Optional[ExceptionModel] = None
    fingerprint: Optional[List[str]] = None
    tags: Optional[Dict[str, str]] = None
    user: Optional[UserModel] = None
    request: Optional[RequestModel] = None
    contexts: Optional[Dict[str, Any]] = None
    breadcrumbs: Optional[List[BreadcrumbModel]] = Field(default=None, max_length=100)
    extra: Optional[Dict[str, Any]] = None
    sdk: SdkModel

    @field_validator("eventId")
    @classmethod
    def _uuid(cls, v: str) -> str:
        assert UUID_V4.match(v), f"eventId {v!r} is not a lowercase uuid v4"
        return v

    @field_validator("timestamp")
    @classmethod
    def _iso(cls, v: str) -> str:
        assert v.endswith("Z")
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return v

    @field_validator("tags")
    @classmethod
    def _tags(cls, v: Optional[Dict[str, str]]) -> Optional[Dict[str, str]]:
        if v is not None:
            assert len(v) <= 50
            assert all(len(k) <= 32 and len(x) <= 200 for k, x in v.items())
        return v

    @field_validator("contexts")
    @classmethod
    def _contexts(cls, v: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        if v is not None:
            if "runtime" in v:
                RuntimeModel.model_validate(v["runtime"])
            if "os" in v:
                OsModel.model_validate(v["os"])
        return v

    @model_validator(mode="after")
    def _depth(self) -> EventModel:
        assert self.exception is None or self.exception.depth() <= 5, "cause depth > 5"
        return self


class BatchModel(Strict):
    events: List[EventModel] = Field(min_length=1, max_length=50)


def assert_conforms(event: Any) -> EventModel:
    return EventModel.model_validate(event)
