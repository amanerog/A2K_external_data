"""FastAPI frontend for a2k-box's deployed MCP -- see frontend/README.md for
how to run this locally. Thin over a2k_client.py: every route here just
turns a form submission into an MCP call and hands the resulting envelope
straight to the browser, same "no business logic in the transport layer"
principle a2k/api/rest.py already follows for a2k-box itself.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from a2k_client import REGION, A2KClientError, call_ask, call_search, new_session

BASE_DIR = Path(__file__).parent

app = FastAPI(title="A2K frontend", description="Local UI over a2k-box's deployed MCP.")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# One boto3 client, reused across requests -- it's a thin HTTP client
# wrapper, not per-request state (unlike the MCP session, which is
# deliberately fresh every call, see a2k_client.py's own docstring).
_bedrock_agentcore_client = boto3.client("bedrock-agentcore", region_name=REGION)


class QueryRequest(BaseModel):
    query: str
    sources: Optional[list[str]] = None


@app.get("/")
async def index(request: Request):
    # Starlette's TemplateResponse signature is (request, name, context=...)
    # in the version this project pins -- request first, not folded into the
    # context dict (the older, still-common-in-docs calling convention).
    return templates.TemplateResponse(request, "index.html")


@app.post("/api/ask")
async def api_ask(req: QueryRequest):
    return _handle(call_ask, req)


@app.post("/api/search")
async def api_search(req: QueryRequest):
    return _handle(call_search, req)


def _handle(call_fn, req: QueryRequest) -> JSONResponse:
    """Shared by both routes. Distinguishes two different kinds of failure:
    a transport-level problem (couldn't reach a2k-box at all, or it didn't
    respond in a well-formed way -- A2KClientError, mapped to 502) vs.
    a2k-box itself answering with `ok: false` (a normal, fully-formed
    CitedResponseEnvelope -- the request to *this* API succeeded, so 200;
    the browser reads `ok`/`error` inside the body, same as it reads any
    other envelope field)."""
    try:
        session_id = new_session(_bedrock_agentcore_client)
        envelope = call_fn(_bedrock_agentcore_client, session_id, req.query, req.sources or None)
        return JSONResponse(envelope)
    except A2KClientError as exc:
        return JSONResponse({"ok": False, "error": {"code": "TRANSPORT_ERROR", "message": str(exc)}}, status_code=502)
