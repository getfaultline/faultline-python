"""Flask integration.

Usage::

    import faultline
    from faultline.integrations.flask import init_app

    faultline.init(api_key=...)
    app = Flask(__name__)
    init_app(app)

``init_app`` wraps ``app.wsgi_app`` (per-request scope + request data) and subscribes to
Flask's ``got_request_exception`` signal, which fires for unhandled exceptions that become a
500 response. ``HTTPException`` (404, 405, ``abort(4xx)``) is never captured.
"""

from __future__ import annotations

from typing import Any

from ..application import get_hub
from ._common import capture_request_exception
from .wsgi import FaultlineWsgiMiddleware

__all__ = ["init_app"]

_INSTALLED_ATTR = "_faultline_installed"


def _on_exception(sender: Any, exception: BaseException, **_: Any) -> None:
    capture_request_exception(get_hub(), exception, "flask")


def init_app(app: Any) -> Any:
    """Instrument a Flask app and return it. Safe to call more than once."""
    from flask import got_request_exception  # lazy: Flask is an optional dependency

    if getattr(app, _INSTALLED_ATTR, False):
        return app
    app.wsgi_app = FaultlineWsgiMiddleware(app.wsgi_app)
    got_request_exception.connect(_on_exception, app, weak=False)
    setattr(app, _INSTALLED_ATTR, True)
    return app
