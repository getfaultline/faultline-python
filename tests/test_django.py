from __future__ import annotations

from typing import Any

import pytest

django = pytest.importorskip("django")

from django.conf import settings  # noqa: E402

if not settings.configured:
    settings.configure(
        DEBUG=False,
        ALLOWED_HOSTS=["testserver"],
        ROOT_URLCONF=__name__,
        SECRET_KEY="test-only",
        MIDDLEWARE=["faultline.integrations.django.FaultlineMiddleware"],
        INSTALLED_APPS=[],
        LOGGING_CONFIG=None,
    )
    django.setup()

from django.http import Http404, HttpResponse  # noqa: E402
from django.test import Client  # noqa: E402
from django.urls import path  # noqa: E402

import faultline  # noqa: E402
from tests.conftest import IngestServer  # noqa: E402

from .contract import assert_conforms  # noqa: E402


def boom(request: Any) -> HttpResponse:
    faultline.set_tag("view", "boom")
    raise ValueError("django failure")


def missing(request: Any) -> HttpResponse:
    raise Http404("nope")


def ok(request: Any) -> HttpResponse:
    faultline.set_tag("leak", "no")
    return HttpResponse("ok")


urlpatterns = [path("boom", boom), path("missing", missing), path("ok", ok)]


def test_unhandled_view_exception_is_captured(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url, integrations={"http": False})
    client = Client(raise_request_exception=False)
    res = client.get("/boom?x=1", HTTP_AUTHORIZATION="Bearer t", HTTP_X_TRACE="abc")
    assert res.status_code == 500
    assert client.get("/missing").status_code == 404
    assert client.get("/ok").status_code == 200
    assert faultline.flush(5)
    assert len(ingest.events) == 1
    ev = ingest.events[0]
    assert ev["exception"]["type"] == "ValueError"
    assert ev["tags"] == {"view": "boom", "mechanism": "django", "handled": "no"}
    req = ev["request"]
    assert req["url"] == "http://testserver/boom?x=1"
    assert req["query"] == {"x": "1"}
    assert req["headers"]["X-Trace"] == "abc"
    assert "Authorization" not in req["headers"]
    assert "leak" not in faultline.get_current_scope().get_data().tags
    assert_conforms(ev)
