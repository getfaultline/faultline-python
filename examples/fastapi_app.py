"""Minimal FastAPI app instrumented with Faultline.

    pip install -e ".[fastapi]" uvicorn
    FAULTLINE_API_KEY=flt_ingest_... uvicorn examples.fastapi_app:app --port 8000
    curl http://127.0.0.1:8000/boom

FAULTLINE_ENDPOINT defaults to http://localhost:4000 (a local faultline-server).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Dict, Optional

from fastapi import FastAPI, HTTPException

import faultline
from faultline.integrations.fastapi import init_app

logging.basicConfig(level=logging.INFO)

faultline.init(
    api_key=os.environ.get("FAULTLINE_API_KEY"),
    endpoint=os.environ.get("FAULTLINE_ENDPOINT", "http://localhost:4000"),
    environment=os.environ.get("FAULTLINE_ENVIRONMENT", "development"),
    release="fastapi-example@0.1.0",
    debug=os.environ.get("FAULTLINE_DEBUG") == "1",
)

app = init_app(FastAPI(title="Faultline FastAPI example"))
log = logging.getLogger("example.fastapi")


@app.get("/")
async def index() -> Dict[str, object]:
    return {"routes": ["/boom", "/message", "/not-found"]}


@app.get("/boom")
async def boom() -> Dict[str, object]:
    faultline.set_tag("feature", "search")
    log.info("querying the index")  # becomes a breadcrumb
    await asyncio.sleep(0)
    raise RuntimeError("search index unavailable")


@app.get("/message")
async def message() -> Dict[str, Optional[str]]:
    return {"event_id": faultline.capture_message("Cache warmed", "info")}


@app.get("/not-found")
async def not_found() -> Dict[str, object]:
    raise HTTPException(status_code=404)  # handled: not captured


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, port=int(os.environ.get("PORT", "8000")))
