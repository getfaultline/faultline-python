from __future__ import annotations

from typing import Any

import pytest

flask = pytest.importorskip("flask")

import faultline  # noqa: E402
from faultline.integrations.flask import init_app  # noqa: E402
from tests.conftest import IngestServer  # noqa: E402

from .contract import assert_conforms  # noqa: E402


def make_app() -> Any:
    app = flask.Flask(__name__)

    @app.route("/ok")
    def ok() -> str:
        faultline.set_tag("route", "ok")
        return "ok"

    @app.route("/boom")
    def boom() -> str:
        faultline.set_user({"id": "u-1"})
        faultline.add_breadcrumb(message="about to fail")
        raise ValueError("flask failure")

    @app.route("/missing")
    def missing() -> str:
        flask.abort(404)
        return ""

    return init_app(app)


def test_unhandled_exception_is_captured_with_request_data(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url, integrations={"http": False})
    client = make_app().test_client()
    res = client.get(
        "/boom?q=shoes&token=abc",
        headers={"Authorization": "Bearer secret", "Cookie": "sid=1", "X-Request-Id": "r1"},
    )
    assert res.status_code == 500
    assert faultline.flush(5)
    assert len(ingest.events) == 1
    ev = ingest.events[0]
    assert ev["exception"]["type"] == "ValueError"
    assert ev["tags"]["mechanism"] == "flask"
    assert ev["user"] == {"id": "u-1"}
    req = ev["request"]
    assert req["method"] == "GET"
    assert req["url"] == "http://localhost/boom?q=shoes&token=[Filtered]"
    assert req["query"] == {"q": "shoes", "token": "[Filtered]"}
    assert "Authorization" not in req["headers"] and "Cookie" not in req["headers"]
    assert req["headers"]["X-Request-Id"] == "r1"
    assert [b.get("message") for b in ev["breadcrumbs"]][-1] == "about to fail"
    assert_conforms(ev)


def test_http_exceptions_and_success_are_not_captured(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url, integrations={"http": False})
    client = make_app().test_client()
    assert client.get("/missing").status_code == 404
    assert client.get("/ok").status_code == 200
    assert faultline.flush(5)
    assert ingest.events == []
    # request scope did not leak into the global scope
    assert "route" not in faultline.get_current_scope().get_data().tags


def test_requests_do_not_share_scope(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url, integrations={"http": False})
    app = make_app()
    client = app.test_client()
    client.get("/ok")
    client.get("/boom")
    assert faultline.flush(5)
    assert "route" not in ingest.events[0].get("tags", {})


def test_init_app_is_idempotent_and_propagation_does_not_double_capture(
    ingest: IngestServer,
) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url, integrations={"http": False})
    app = make_app()
    init_app(app)
    app.config["PROPAGATE_EXCEPTIONS"] = True
    with pytest.raises(ValueError):
        app.test_client().get("/boom")
    assert faultline.flush(5)
    assert len(ingest.events) == 1
