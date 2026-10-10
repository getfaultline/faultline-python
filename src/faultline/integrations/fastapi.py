"""FastAPI / Starlette integration.

Usage::

    import faultline
    from faultline.integrations.fastapi import init_app

    faultline.init(api_key=...)
    app = FastAPI()
    init_app(app)

Unhandled exceptions are captured with the request's data and scope. ``HTTPException`` and
other exceptions handled by your exception handlers are not captured.
"""

from __future__ import annotations

from typing import Any

from .asgi import FaultlineAsgiMiddleware

__all__ = ["FaultlineAsgiMiddleware", "init_app"]


def init_app(app: Any) -> Any:
    """Add the Faultline ASGI middleware to a FastAPI or Starlette app and return the app."""
    app.add_middleware(FaultlineAsgiMiddleware)
    return app
