"""Minimal Flask app instrumented with Faultline.

    pip install -e ".[flask]"
    FAULTLINE_API_KEY=flt_ingest_... python examples/flask_app.py
    curl http://127.0.0.1:5000/boom

FAULTLINE_ENDPOINT defaults to http://localhost:4000 (a local faultline-server).
"""

from __future__ import annotations

import logging
import os

from flask import Flask, jsonify

import faultline
from faultline.integrations.flask import init_app

logging.basicConfig(level=logging.INFO)

faultline.init(
    api_key=os.environ.get("FAULTLINE_API_KEY"),
    endpoint=os.environ.get("FAULTLINE_ENDPOINT", "http://localhost:4000"),
    environment=os.environ.get("FAULTLINE_ENVIRONMENT", "development"),
    release="flask-example@0.1.0",
    debug=os.environ.get("FAULTLINE_DEBUG") == "1",
)

app = init_app(Flask(__name__))
log = logging.getLogger("example.flask")


@app.get("/")
def index() -> object:
    return jsonify(routes=["/boom", "/message", "/handled"])


@app.get("/boom")
def boom() -> object:
    faultline.set_user({"id": "user-42", "email": "ada@example.com"})
    faultline.set_tag("feature", "checkout")
    log.info("about to divide by zero")  # becomes a breadcrumb
    return jsonify(result=1 / 0)


@app.get("/message")
def message() -> object:
    event_id = faultline.capture_message("Payment webhook retried", "warning")
    return jsonify(event_id=event_id)


@app.get("/handled")
def handled() -> object:
    try:
        int("not a number")
    except ValueError:
        event_id = faultline.capture_exception(tags={"handled": "yes"})
        return jsonify(event_id=event_id), 400
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(port=int(os.environ.get("PORT", "5000")))
