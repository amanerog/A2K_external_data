"""FastAPI frontend for a2k-box's deployed MCP -- see frontend/README.md for
how to run this locally. Thin over a2k_client.py: every route here just
turns a form submission into an MCP call and hands the resulting envelope
straight to the browser, same "no business logic in the transport layer"
principle a2k/api/rest.py already follows for a2k-box itself.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.parse import parse_qs
from pathlib import Path
from typing import Optional

import boto3
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from a2k_client import REGION, A2KClientError, call_ask, call_search, new_session
from passwords import hash_password, verify_password

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


# HTTP Basic auth, one user and password per person, on when users are
# configured: a Lambda Function URL (deploy/lambda/) is reachable from
# anywhere, and every question asked here runs with this app's own IAM
# permissions on a2k-box. Users live in Secrets Manager
# (A2K_FRONTEND_PASSWORD_SECRET_ARN, created by the Lambda stack, managed with
# deploy/lambda/users.py) as {"users": {"<name>": "<pbkdf2 hash>"}} -- never a
# password in clear. Reloaded every A2K_FRONTEND_USERS_TTL seconds, so adding
# or removing someone needs no redeploy. A secret that is still a single
# plain password (the stack's first version) is user A2K_FRONTEND_USER; for
# local runs, A2K_FRONTEND_PASSWORD does the same. /health stays open.
AUTH_USER = os.environ.get("A2K_FRONTEND_USER", "a2k")
USERS_TTL = float(os.environ.get("A2K_FRONTEND_USERS_TTL", "300"))
_users: dict = {"loaded_at": None, "hashes": {}, "verified": set()}


def _read_users() -> dict[str, str]:
    """{user: password hash}; empty means no auth."""
    secret_arn = os.environ.get("A2K_FRONTEND_PASSWORD_SECRET_ARN")
    if secret_arn:
        raw = boto3.client("secretsmanager", region_name=REGION).get_secret_value(SecretId=secret_arn)["SecretString"]
    else:
        raw = os.environ.get("A2K_FRONTEND_PASSWORD") or ""
    if not raw:
        return {}
    try:
        users = json.loads(raw)["users"]
        return {str(k): str(v) for k, v in users.items()}
    except (ValueError, KeyError, TypeError, AttributeError):
        return {AUTH_USER: hash_password(raw, iterations=1_000)}  # legacy: one shared plain password


def _current_users() -> dict[str, str]:
    now = time.monotonic()
    if _users["loaded_at"] is None or now - _users["loaded_at"] > USERS_TTL:
        _users.update(loaded_at=now, hashes=_read_users(), verified=set())
    return _users["hashes"]


def _authenticate(header: str | None, users: dict[str, str]) -> str | None:
    """The user name, if the Basic credentials match a configured user."""
    if not header or not header.startswith("Basic "):
        return None
    try:
        user, _, given = base64.b64decode(header[6:]).decode("utf-8").partition(":")
    except ValueError:
        return None
    stored = users.get(user)
    if stored is None:
        return None
    # PBKDF2 is slow on purpose; a credential checked once is remembered for
    # this warm instance until the user list is reloaded.
    key = hashlib.sha256(f"{user}\0{given}\0{stored}".encode()).hexdigest()
    if key in _users["verified"]:
        return user
    if verify_password(given, stored):
        _users["verified"].add(key)
        return user
    return None


def _log(record: dict) -> None:
    print(json.dumps(record, ensure_ascii=False), flush=True)


# Browser sessions. A Lambda Function URL renames a response's
# WWW-Authenticate header (x-amzn-Remapped-www-authenticate, seen live
# 2026-10-14), so a browser never shows its Basic-auth prompt there: people
# sign in on /login instead and get a session cookie. The cookie is signed
# with a key derived from that user's own stored hash, so `users.py reset`
# or `remove` ends that user's sessions and nobody else's. Basic auth still
# works for scripts (curl -u).
SESSION_COOKIE = "a2k_session"
SESSION_SECONDS = int(float(os.environ.get("A2K_FRONTEND_SESSION_HOURS", "12")) * 3600)
OPEN_PATHS = ("/health", "/login", "/logout")


def _session_signature(user: str, expires: int, stored_hash: str) -> str:
    key = hashlib.sha256(f"a2k-session\0{stored_hash}".encode()).digest()
    return hmac.new(key, f"{user}\0{expires}".encode(), hashlib.sha256).hexdigest()


def _session_cookie(user: str, stored_hash: str) -> str:
    expires = int(time.time()) + SESSION_SECONDS
    name = base64.urlsafe_b64encode(user.encode()).decode().rstrip("=")
    return f"{name}.{expires}.{_session_signature(user, expires, stored_hash)}"


def _session_user(cookie: str | None, users: dict[str, str]) -> str | None:
    try:
        name, expires, signature = (cookie or "").split(".")
        user = base64.urlsafe_b64decode(name + "=" * (-len(name) % 4)).decode()
        stored = users.get(user)
        if stored is None or int(expires) < time.time():
            return None
        return user if hmac.compare_digest(signature, _session_signature(user, int(expires), stored)) else None
    except (ValueError, UnicodeDecodeError):
        return None


@app.middleware("http")
async def require_user(request: Request, call_next):
    request.state.user = None
    path = request.url.path
    if path in OPEN_PATHS or path.startswith("/static/"):
        return await call_next(request)
    users = _current_users()
    if users:
        user = _session_user(request.cookies.get(SESSION_COOKIE), users) or _authenticate(request.headers.get("authorization"), users)
        if user is None:
            if request.headers.get("authorization"):
                _log({"event": "frontend.auth_failed", "path": path, "client": request.client.host if request.client else None})
            if path.startswith("/api/"):
                return JSONResponse({"ok": False, "error": {"code": "UNAUTHENTICATED", "message": "Sign in again."}}, status_code=401)
            return RedirectResponse(f"{BASE_PATH}/login", status_code=303)
        request.state.user = user
    return await call_next(request)


@app.get("/login")
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"base_path": BASE_PATH, "error": False})


@app.post("/login")
async def login(request: Request):
    form = parse_qs((await request.body()).decode("utf-8"))
    user, password = (form.get("user") or [""])[0].strip(), (form.get("password") or [""])[0]
    users = _current_users()
    stored = users.get(user)
    if stored is None or not verify_password(password, stored):
        _log({"event": "frontend.auth_failed", "path": "/login", "user_attempted": user[:64], "client": request.client.host if request.client else None})
        return templates.TemplateResponse(request, "login.html", {"base_path": BASE_PATH, "error": True}, status_code=401)
    _log({"event": "frontend.login", "user": user})
    response = RedirectResponse(f"{BASE_PATH}/", status_code=303)
    secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
    response.set_cookie(SESSION_COOKIE, _session_cookie(user, stored), max_age=SESSION_SECONDS, path=BASE_PATH or "/",
                        httponly=True, secure=secure, samesite="lax")
    return response


@app.get("/logout")
async def logout():
    response = RedirectResponse(f"{BASE_PATH}/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE, path=BASE_PATH or "/")
    return response
    users = _current_users()
    if users:
        user = _authenticate(request.headers.get("authorization"), users)
        if user is None:
            if request.headers.get("authorization"):
                _log({"event": "frontend.auth_failed", "path": request.url.path, "client": request.client.host if request.client else None})
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="A2K", charset="UTF-8"'})
        request.state.user = user
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
    return templates.TemplateResponse(request, "index.html", {"base_path": BASE_PATH, "user": request.state.user})


@app.post("/api/ask")
async def api_ask(req: QueryRequest, request: Request):
    return _handle(call_ask, req, request.state.user, "ask")


@app.post("/api/search")
async def api_search(req: QueryRequest, request: Request):
    return _handle(call_search, req, request.state.user, "search")


def _handle(call_fn, req: QueryRequest, user: str | None = None, operation: str = "ask") -> JSONResponse:
    """Shared by both routes. Distinguishes two different kinds of failure:
    a transport-level problem (couldn't reach a2k-box at all, or it didn't
    respond in a well-formed way -- A2KClientError, mapped to 502) vs.
    a2k-box itself answering with `ok: false` (a normal, fully-formed
    CitedResponseEnvelope -- the request to *this* API succeeded, so 200;
    the browser reads `ok`/`error` inside the body, same as it reads any
    other envelope field)."""
    t0 = time.monotonic()
    try:
        session_id = new_session(_bedrock_agentcore_client)
        envelope = call_fn(_bedrock_agentcore_client, session_id, req.query, req.sources or None, user=user)
        status = 200
    except A2KClientError as exc:
        envelope = {"ok": False, "error": {"code": "TRANSPORT_ERROR", "message": str(exc)}}
        status = 502
    # One line per question, for the trail: who asked what (by fingerprint --
    # the same query_sha256 a2k-box's own logs carry), and what came back;
    # requestId joins it to a2k-box's audit record.
    _log({
        "event": "frontend.request",
        "user": user,
        "operation": operation,
        "query_sha256": hashlib.sha256(req.query.encode("utf-8")).hexdigest()[:16],
        "sources": req.sources or None,
        "status": status,
        "ok": envelope.get("ok"),
        "error_code": (envelope.get("error") or {}).get("code"),
        "request_id": (envelope.get("audit") or {}).get("requestId"),
        "served_from_cache": "cache" in ((envelope.get("audit") or {}).get("decisionReason") or "").lower(),
        "ms": int((time.monotonic() - t0) * 1000),
    })
    return JSONResponse(envelope, status_code=status)
