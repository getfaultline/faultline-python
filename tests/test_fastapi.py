from __future__ import annotations

import asyncio
from typing import Any

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

import faultline  # noqa: E402
from faultline.integrations.asgi import FaultlineAsgiMiddleware  # noqa: E402
from faultline.integrations.fastapi import init_app  # noqa: E402
from tests.conftest import IngestServer  # noqa: E402

from .contract import assert_conforms  # noqa: E402


def make_app(instrument: bool = True) -> Any:
    app = fastapi.FastAPI()

    @app.get("/boom/{item}")
    async def boom(item: str) -> dict[str, str]:
        faultline.set_tag("item", item)
        await asyncio.sleep(0)
        raise RuntimeError(f"failed {item}")

    @app.get("/sync-boom")
    def sync_boom() -> dict[str, str]:
        faultline.set_tag("sync", "yes")
        raise LookupError("sync failure")

    @app.get("/teapot")
    async def teapot() -> dict[str, str]:
        raise fastapi.HTTPException(status_code=418)

    @app.get("/ok")
    async def ok() -> dict[str, str]:
        faultline.set_tag("leak", "no")
        return {"ok": "yes"}

    return init_app(app) if instrument else app


def test_unhandled_exception_is_captured(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url, integrations={"http": False})
    client = TestClient(make_app(), raise_server_exceptions=False)
    res = client.get("/boom/a1?page=2", headers={"Authorization": "x", "X-Api-Key": "k"})
    assert res.status_code == 500
    assert faultline.flush(5)
    ev = ingest.events[0]
    assert ev["exception"]["type"] == "RuntimeError"
    assert ev["tags"] == {"item": "a1", "mechanism": "asgi", "handled": "no"}
    req = ev["request"]
    assert req["method"] == "GET"
    assert req["url"] == "http://testserver/boom/a1?page=2"
    assert req["query"] == {"page": "2"}
    assert "Authorization" not in req["headers"]
    assert req["headers"]["X-Api-Key"] == "[Filtered]"
    assert_conforms(ev)


def test_sync_endpoints_in_the_threadpool_keep_the_request_scope(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url, integrations={"http": False})
    client = TestClient(make_app(), raise_server_exceptions=False)
    assert client.get("/sync-boom").status_code == 500
    assert faultline.flush(5)
    ev = ingest.events[0]
    assert ev["tags"]["sync"] == "yes"
    assert ev["request"]["url"].endswith("/sync-boom")


def test_http_exceptions_are_not_captured_and_scope_does_not_leak(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url, integrations={"http": False})
    client = TestClient(make_app(), raise_server_exceptions=False)
    assert client.get("/teapot").status_code == 418
    assert client.get("/ok").status_code == 200
    assert client.get("/boom/b2").status_code == 500
    assert faultline.flush(5)
    assert len(ingest.events) == 1
    assert "leak" not in ingest.events[0]["tags"]


def test_concurrent_requests_are_isolated(ingest: IngestServer) -> None:
    faultline.init(api_key="flt_ingest_test", endpoint=ingest.url, integrations={"http": False})
    app = FaultlineAsgiMiddleware(make_app(instrument=False))  # plain ASGI wrapping also works
    import httpx

    async def run() -> None:
        transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
            await asyncio.gather(*(c.get(f"/boom/i{i}") for i in range(5)))

    asyncio.run(run())
    assert faultline.flush(5)
    assert len(ingest.events) == 5
    for ev in ingest.events:
        assert ev["exception"]["value"] == f"failed {ev['tags']['item']}"
        assert ev["request"]["url"].endswith(f"/boom/{ev['tags']['item']}")


def test_lifespan_and_non_http_scopes_pass_through() -> None:
    called: list[str] = []

    async def app(scope: Any, receive: Any, send: Any) -> None:
        called.append(scope["type"])

    asyncio.run(FaultlineAsgiMiddleware(app)({"type": "lifespan"}, None, None))  # type: ignore[arg-type]
    assert called == ["lifespan"]
