"""HTTPS+JSON transport binding (A2K-KCP-Consumption 4.md, section 2).

Thin over `gateway/engine.py`: every route here parses a request model,
calls the engine, and serializes the resulting envelope. No business logic
lives in this module.
"""

from __future__ import annotations

import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import ValidationError

from ..cards import load_card
from ..errors import A2KError, ErrorCode
from ..gateway.engine import engine
from ..models.request import A2KRequest, ExplainRequest, GetDocumentRequest

app = FastAPI(
    title="A2K Box -- Cala + Sayari gateway",
    version="0.1.0",
    description="A2K-KCP cited-response gateway fronting Cala and Sayari for the K2 agent.",
)

SOURCES = {"cala", "sayari"}
OPERATIONS = {"search", "ask", "explain", "getDocument"}

_ERROR_STATUS = {
    ErrorCode.INVALID_REQUEST: 400,
    ErrorCode.UNSUPPORTED_VERSION: 400,
    ErrorCode.UNSUPPORTED_OPERATION: 400,
    ErrorCode.SCHEMA_VALIDATION_FAILED: 400,
    ErrorCode.AMBIGUOUS_QUERY: 400,
    ErrorCode.AUTHENTICATION_REQUIRED: 401,
    ErrorCode.AUTHORIZATION_FAILED: 403,
    ErrorCode.OBO_ASSERTION_REQUIRED: 403,
    ErrorCode.OBO_ASSERTION_INVALID: 403,
    ErrorCode.ACCESS_DENIED: 403,
    ErrorCode.NOT_FOUND: 404,
    ErrorCode.OUT_OF_SCOPE: 404,
    ErrorCode.PAGINATION_CURSOR_INVALID: 400,
    ErrorCode.RATE_LIMITED: 429,
    ErrorCode.GROUNDING_VIOLATION: 422,
    ErrorCode.STRICT_GROUNDING_UNSUPPORTED: 422,
    ErrorCode.CONFLICT_DETECTED: 409,
    ErrorCode.SIGNATURE_REQUIRED: 400,
    ErrorCode.SIGNATURE_INVALID: 400,
    ErrorCode.REPLAY_DETECTED: 409,
    ErrorCode.REGULATED_FEATURE_UNSUPPORTED: 501,
    ErrorCode.TIMEOUT: 504,
    ErrorCode.UPSTREAM_ERROR: 502,
}


def _status_for(envelope) -> int:
    if envelope.ok or envelope.error is None:
        return 200
    try:
        return _ERROR_STATUS.get(ErrorCode(envelope.error.code), 500)
    except ValueError:
        return 500


@app.get("/.well-known/a2k-card.json")
async def well_known_card() -> dict:
    return load_card("gateway").model_dump(mode="json")


@app.get("/a2k/{source}/card")
async def source_card(source: str) -> dict:
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source!r}. Known: {sorted(SOURCES)}")
    return load_card(source).model_dump(mode="json")


async def _parse_body(request: Request) -> dict | JSONResponse:
    """Parse the request body as JSON, or a ready-to-return 400 on failure.

    ``Request.json()`` raises ``json.JSONDecodeError`` on an empty or
    malformed body, which FastAPI does not translate into the A2K error
    envelope -- left uncaught it surfaces as an unhandled 500.
    """
    try:
        return await request.json()
    except json.JSONDecodeError as exc:
        err = A2KError(ErrorCode.SCHEMA_VALIDATION_FAILED, f"Invalid JSON body: {exc}")
        return JSONResponse(status_code=400, content={"ok": False, "error": err.to_dict()})


async def _dispatch(operation: str, body: dict, sources: list[str] | None) -> JSONResponse:
    if operation not in OPERATIONS:
        err = A2KError(ErrorCode.UNSUPPORTED_OPERATION, f"Unsupported operation: {operation!r}.")
        return JSONResponse(status_code=400, content={"ok": False, "error": err.to_dict()})

    try:
        if operation == "search":
            req = A2KRequest.model_validate({**body, "operation": "search"})
            if sources is not None:
                req.sources = sources
            envelope = await engine.search(req)
        elif operation == "ask":
            req = A2KRequest.model_validate({**body, "operation": "ask"})
            if sources is not None:
                req.sources = sources
            envelope = await engine.ask(req)
        elif operation == "explain":
            req = ExplainRequest.model_validate(body)
            envelope = await engine.explain(req)
        else:
            req = GetDocumentRequest.model_validate(body)
            envelope = await engine.get_document(req)
    except ValidationError as exc:
        err = A2KError(ErrorCode.SCHEMA_VALIDATION_FAILED, str(exc))
        return JSONResponse(status_code=400, content={"ok": False, "error": err.to_dict()})
    except A2KError as exc:
        return JSONResponse(
            status_code=_ERROR_STATUS.get(exc.code, 500),
            content={"ok": False, "error": exc.to_dict()},
        )

    return JSONResponse(status_code=_status_for(envelope), content=envelope.model_dump(mode="json"))


@app.post("/a2k/streamAsk")
async def stream_ask(request: Request) -> StreamingResponse:
    """Deferred-verification streaming (A2K-KCP-Consumption 4.md, section 16).

    Simplification (documented in README): this gateway does not generate
    `answer` token-by-token -- it computes the full envelope first, then
    chunks the already-final text over SSE. The `proof_footer` is real: it
    carries the same claims/citations/grounding/signature a non-streaming
    `ask` would return, so a client validating the footer gets a genuine
    verification, just not genuine incremental generation.
    """
    body = await _parse_body(request)
    if isinstance(body, JSONResponse):
        return body
    try:
        req = A2KRequest.model_validate({**body, "operation": "ask"})
    except ValidationError as exc:
        err = A2KError(ErrorCode.SCHEMA_VALIDATION_FAILED, str(exc))
        return JSONResponse(status_code=400, content={"ok": False, "error": err.to_dict()})

    envelope = await engine.ask(req)

    async def event_stream():
        if envelope.answer:
            chunk_size = 80
            for i in range(0, len(envelope.answer), chunk_size):
                chunk = envelope.answer[i : i + chunk_size]
                yield f"event: text_chunk\ndata: {json.dumps({'text': chunk})}\n\n"
        footer = {
            "a2kVersion": envelope.a2kVersion,
            "ok": envelope.ok,
            "sourceKbId": envelope.sourceKbId,
            "claims": [c.model_dump(mode="json") for c in envelope.claims],
            "citations": [c.model_dump(mode="json") for c in envelope.citations],
            "grounding": envelope.grounding.model_dump(mode="json") if envelope.grounding else None,
            "accessDecision": envelope.accessDecision.model_dump(mode="json") if envelope.accessDecision else None,
            "audit": envelope.audit.model_dump(mode="json") if envelope.audit else None,
            "responseSignature": envelope.responseSignature.model_dump(mode="json")
            if envelope.responseSignature
            else None,
        }
        yield f"event: proof_footer\ndata: {json.dumps(footer)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@app.post("/a2k/{operation}")
async def gateway_operation(operation: str, request: Request) -> JSONResponse:
    body = await _parse_body(request)
    if isinstance(body, JSONResponse):
        return body
    return await _dispatch(operation, body, sources=None)


@app.post("/a2k/{source}/{operation}")
async def source_operation(source: str, operation: str, request: Request) -> JSONResponse:
    if source not in SOURCES:
        raise HTTPException(status_code=404, detail=f"Unknown source: {source!r}. Known: {sorted(SOURCES)}")
    body = await _parse_body(request)
    if isinstance(body, JSONResponse):
        return body
    return await _dispatch(operation, body, sources=[source])


@app.get("/")
@app.get("/health")
async def health() -> dict:
    return {"service": "a2k-box", "status": "ok", "sources": sorted(SOURCES)}
