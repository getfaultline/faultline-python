"""Integrations hook the SDK into the host runtime and frameworks.

Built-in (configured with ``faultline.init(integrations={...})``): ``excepthook``,
``threading``, ``logging`` and ``http``. Framework integrations live in their own modules
and import their framework lazily: ``faultline.integrations.django``, ``.flask``,
``.fastapi``, ``.asgi`` and ``.wsgi``.
"""

from .excepthook import ExcepthookIntegration, ThreadingIntegration
from .logging import LoggingIntegration
from .stdlib_http import StdlibHttpIntegration

__all__ = [
    "ExcepthookIntegration",
    "LoggingIntegration",
    "StdlibHttpIntegration",
    "ThreadingIntegration",
]
