"""Scope: the contextual data applied to every event captured while it is active.

Pure data structure with no I/O and no context-variable handling; the Hub decides which scope
is current. Every mutation replaces the underlying container instead of changing it in place
(copy-on-write), so ``clone()`` and ``get_data()`` are O(1) and a snapshot taken by one thread
can never be mutated by another.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Dict, List, Mapping, Optional, Tuple

from .event import Breadcrumb, RequestData, User
from .limits import MAX_BREADCRUMBS

_EMPTY: Mapping[str, Any] = MappingProxyType({})


@dataclass(frozen=True)
class ScopeData:
    """Immutable snapshot of a scope, taken when an event is captured."""

    tags: Mapping[str, str]
    user: Optional[User]
    contexts: Mapping[str, Mapping[str, Any]]
    extra: Mapping[str, Any]
    breadcrumbs: Tuple[Breadcrumb, ...]
    level: Optional[str]
    fingerprint: Optional[Tuple[str, ...]]
    request: Optional[RequestData]


class Scope:
    __slots__ = (
        "_breadcrumbs",
        "_contexts",
        "_extra",
        "_fingerprint",
        "_level",
        "_request",
        "_tags",
        "_user",
    )

    def __init__(self) -> None:
        self._tags: Mapping[str, str] = _EMPTY
        self._user: Optional[User] = None
        self._contexts: Mapping[str, Mapping[str, Any]] = _EMPTY
        self._extra: Mapping[str, Any] = _EMPTY
        self._breadcrumbs: Tuple[Breadcrumb, ...] = ()
        self._level: Optional[str] = None
        self._fingerprint: Optional[Tuple[str, ...]] = None
        self._request: Optional[RequestData] = None

    def clone(self) -> Scope:
        s = Scope()
        s._tags = self._tags
        s._user = self._user
        s._contexts = self._contexts
        s._extra = self._extra
        s._breadcrumbs = self._breadcrumbs
        s._level = self._level
        s._fingerprint = self._fingerprint
        s._request = self._request
        return s

    def set_tag(self, key: str, value: str) -> Scope:
        self._tags = {**self._tags, key: value}
        return self

    def set_tags(self, tags: Mapping[str, str]) -> Scope:
        self._tags = {**self._tags, **tags}
        return self

    def remove_tag(self, key: str) -> Scope:
        self._tags = {k: v for k, v in self._tags.items() if k != key}
        return self

    def set_user(self, user: Optional[User]) -> Scope:
        """``None`` clears the user."""
        self._user = User(**user) if user else None
        return self

    def set_context(self, name: str, context: Optional[Mapping[str, Any]]) -> Scope:
        """``None`` removes the context."""
        contexts = dict(self._contexts)
        if context is None:
            contexts.pop(name, None)
        else:
            contexts[name] = dict(context)
        self._contexts = contexts
        return self

    def set_extra(self, key: str, value: Any) -> Scope:
        self._extra = {**self._extra, key: value}
        return self

    def set_extras(self, extras: Mapping[str, Any]) -> Scope:
        self._extra = {**self._extra, **extras}
        return self

    def set_level(self, level: Optional[str]) -> Scope:
        self._level = level
        return self

    def set_fingerprint(self, fingerprint: Optional[List[str]]) -> Scope:
        self._fingerprint = tuple(fingerprint) if fingerprint else None
        return self

    def set_request(self, request: Optional[RequestData]) -> Scope:
        self._request = RequestData(**request) if request else None
        return self

    def add_breadcrumb(self, crumb: Breadcrumb, max_breadcrumbs: int = MAX_BREADCRUMBS) -> Scope:
        cap = max(0, min(max_breadcrumbs, MAX_BREADCRUMBS))
        if cap == 0:
            return self
        crumbs = (*self._breadcrumbs, crumb)
        self._breadcrumbs = crumbs[-cap:]
        return self

    def clear_breadcrumbs(self) -> Scope:
        self._breadcrumbs = ()
        return self

    def clear(self) -> Scope:
        self.__init__()  # type: ignore[misc]
        return self

    @property
    def user(self) -> Optional[User]:
        return self._user

    @property
    def request(self) -> Optional[RequestData]:
        return self._request

    def get_data(self) -> ScopeData:
        return ScopeData(
            tags=self._tags,
            user=self._user,
            contexts=self._contexts,
            extra=self._extra,
            breadcrumbs=self._breadcrumbs,
            level=self._level,
            fingerprint=self._fingerprint,
            request=self._request,
        )


def merge_dicts(*parts: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for part in parts:
        if part:
            out.update(part)
    return out
