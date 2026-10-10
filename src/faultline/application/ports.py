"""Ports: the interfaces the application layer depends on. Infrastructure implements them."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Protocol

from ..domain import Event, StackFrame

#: Name of the transport's worker thread. Integrations use it to skip the SDK's own traffic.
TRANSPORT_THREAD_NAME = "faultline-transport"


class Transport(Protocol):
    """Delivers events to the Faultline ingest API. Must never raise."""

    def send(self, event: Event) -> None:
        """Enqueue an event for delivery (non-blocking)."""

    def flush(self, timeout: float = 2.0) -> bool:
        """True when everything queued so far was delivered (or dropped) within ``timeout``."""

    def close(self, timeout: float = 2.0) -> bool:
        """Flush, then stop accepting events and stop the worker."""


class StackExtractor(Protocol):
    """Turns tracebacks into contract frames ordered oldest to newest. Must never raise."""

    def from_exception(self, exc: BaseException) -> List[StackFrame]: ...

    def from_current_stack(self) -> List[StackFrame]:
        """Frames of the calling code (SDK frames removed), for non-exception values."""


class SourceContextReader(Protocol):
    """Adds pre/post/context lines to in-app frames. Returns the input on failure."""

    def add_source_context(self, frames: List[StackFrame]) -> List[StackFrame]: ...


class Clock(Protocol):
    def now(self) -> float:
        """Epoch seconds."""

    def monotonic(self) -> float: ...


class IdGenerator(Protocol):
    def uuid(self) -> str:
        """Lowercase uuid v4 with dashes."""


class RandomSource(Protocol):
    def random(self) -> float:
        """Uniform in [0, 1)."""


class Logger(Protocol):
    def debug(self, message: str, *args: Any) -> None: ...

    def warn(self, message: str, *args: Any) -> None: ...

    def error(self, message: str, *args: Any) -> None: ...


class RuntimeInfo(Protocol):
    def contexts(self) -> Dict[str, Any]:
        """``{"runtime": {"name", "version"}, "os": {"name", "version"}}``."""

    def server_name(self) -> Optional[str]: ...


class SilentLogger:
    def debug(self, message: str, *args: Any) -> None:
        pass

    def warn(self, message: str, *args: Any) -> None:
        pass

    def error(self, message: str, *args: Any) -> None:
        pass
