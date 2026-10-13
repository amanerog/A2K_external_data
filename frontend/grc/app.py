"""FastAPI frontend for a2k-box's deployed MCP -- see frontend/README.md for
how to run this locally. Thin over a2k_client.py: every route here just
turns a form submission into an MCP call and hands the resulting envelope
straight to the browser, same "no business logic in the transport layer"
principle a2k/api/rest.py already follows for a2k-box itself.
"""

from __future__ import annotations

import base64
import hmac
import os
from functools import lru_cache
from pathlib import Path
from typing import Optional

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from a2k_client import REGION, A2KClientError, call_ask, call_search, new_session

BASE_DIR = Path(__file__).parent
# The path prefix the page is published under when an ingress routes a
# sub-path to it and strips it before forwarding (e.g. "/a2kfrontexternaldata"
# with nginx's rewrite-target). The app itself always serves from "/"; only
# the URLs the browser builds -- static files and the JSON API -- need it.
BASE_PATH = os.environ.get("A2K_FRONTEND_BASE_PATH", "").rstrip("/")

app = FastAPI(title="A2K frontend", description="Local UI over a2k-box's deployed MCP.")
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")
templates = Jinja2Templates(directory=BASE_DIR / "templates")

# One boto3 client, reused across requests -- it's a thin HTTP client
# wrapper, not per-request state (unlike the MCP session, which is
# deliberately fresh every call, see a2k_client.py's own docstring).
_bedrock_agentcore_client = boto3.client("bedrock-agentcore", region_name=REGION)


# HTTP Basic auth, on when a password is configured: a Lambda Function URL
# (deploy/lambda/) is reachable from anywhere, and every question asked here
# runs with this app's own IAM permissions on a2k-box. The password comes
# from Secrets Manager (A2K_FRONTEND_PASSWORD_SECRET_ARN, the Lambda stack
# creates it) or, for local runs, A2K_FRONTEND_PASSWORD. /health stays open.
AUTH_USER = os.environ.get("A2K_FRONTEND_USER", "a2k")


@lru_cache(maxsize=1)
def _password() -> str | None:
    secret_arn = os.environ.get("A2K_FRONTEND_PASSWORD_SECRET_ARN")
    if secret_arn:
        return boto3.client("secretsmanager", region_name=REGION).get_secret_value(SecretId=secret_arn)["SecretString"]
    return os.environ.get("A2K_FRONTEND_PASSWORD") or None


def _authorized(header: str | None, password: str) -> bool:
    if not header or not header.startswith("Basic "):
        return False
    try:
        user, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except ValueError:
        return False
    return hmac.compare_digest(user, AUTH_USER) and hmac.compare_digest(given, password)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    password = _password()
    if password and request.url.path != "/health" and not _authorized(request.headers.get("authorization"), password):
        return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="A2K", charset="UTF-8"'})
    return await call_next(request)


class QueryRequest(BaseModel):
    query: str
    sources: Optional[list[str]] = None


@app.get("/health")
async def health() -> dict:
    # Deliberately doesn't call out to a2k-box's own Runtime -- this only
    # confirms the frontend process itself is up, same shallow check
    # a2k/api/rest.py's own /health does. A deep check (a live MCP round
    # trip) would make k8s restart this pod whenever a2k-box is briefly
    # unreachable, which is a2k-box's outage to surface, not this frontend's.
    return {"service": "a2k-frontend", "status": "ok"}


@app.get("/")
async def index(request: Request):
    # Starlette's TemplateResponse signature is (request, name, context=...)
    # in the version this project pins -- request first, not folded into the
    # context dict (the older, still-common-in-docs calling convention).
    return templates.TemplateResponse(request, "index.html", {"base_path": BASE_PATH})


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
