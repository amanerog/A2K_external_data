"""MCP transport binding, A2K-KCP 0.1 section 10.2: the surface K2 calls.

- Tools a2k.ask and a2k.search only: arguments are the 0.1 request, the
  result's structuredContent is the 0.1 envelope, declared as outputSchema
  (the reference package's self-contained response bundles), isError false
  for success and failure alike. The mapping lives in a2k/kcp01/.
- Resource a2k://card: the service's 0.1 KB Card (a2k/cards/kcp01/). The
  vendors behind it are internal and never named here.
- The pre-0.1 tools (vendor-forced ask/search, listVendors, explain,
  getDocument, validateCitation, reportConflict, getAuditRecord, getCard) and
  the per-vendor card resources are registered as `internal.*` only with
  A2K_INTERNAL_TOOLS=true -- for our evaluation scripts on test runtimes.

Run standalone with `python -m a2k.mcp_server` (streamable-http transport,
listening on 0.0.0.0:8000/mcp -- the path/port AgentCore Runtime expects
when hosting an MCP server, see README "Deploy to AgentCore").
"""

from __future__ import annotations

import json
from typing import Any

import anyio
import uvicorn
from mcp.server.fastmcp import Context, FastMCP
from starlette.responses import JSONResponse
from starlette.routing import Route

from ..cache import admission
from ..cards import load_card, vendor_catalogue
from ..config import config
from ..gateway.engine import engine
from ..kcp01 import adapter as kcp01
from ..kcp01.card import service_card
from ..models.envelope import CitedResponseEnvelope
from ..models.request import A2KRequest, ExplainRequest, GetDocumentRequest, OnBehalfOf, RequestMetadata, Requirements

class _KcpFastMCP(FastMCP):
    """FastMCP builds a tool's arguments from the function signature and drops
    any member it does not know, so an A2K 0.1 request with an unknown core
    member would be answered instead of rejected (SCHEMA_VALIDATION_FAILED,
    section 2.1). a2k.ask and a2k.search are therefore handed the arguments
    exactly as sent; the reference request models, which are closed, judge
    them. The result goes through the tool's own conversion, so the response
    is shaped exactly as before (structuredContent + text)."""

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        handler = {"a2k.ask": kcp01.ask, "a2k.search": kcp01.search}.get(name)
        if handler is None:
            return await super().call_tool(name, arguments)
        envelope = await handler(dict(arguments or {}), session_id=_session_id(None, self.get_context()))
        return self._tool_manager.get_tool(name).fn_metadata.convert_result(envelope)


mcp = _KcpFastMCP(
    name="a2k-box",
    # Read by any MCP client at `initialize` -- this is the closest thing this
    # contract has to a "system prompt" that ships with the protocol itself,
    # rather than living in one particular caller's own prompt engineering.
    # Written for an agent with *no* prior context about this gateway (not
    # just K2): before this was tightened, our own router agent's system
    # prompt carried this same guidance separately (agent/core.py), which an
    # external MCP client calling a2k-box directly would never see. Anything
    # a caller needs to use this contract correctly belongs here or in the
    # individual tool docstrings below, not in any one consumer's prompt.
    instructions=(
        "A2K-KCP 0.1 knowledge base of external, third-party information on companies, "
        "people, markets and regulation. Read the KB Card at resource a2k://card. "
        "Call a2k.ask for a cited answer to a question, a2k.search for cited passages. "
        "Both take the A2K 0.1 request (a2kVersion \"0.1\", operation, requestId, query, "
        "and optionally context, requirements, filters; pagination on search) and return "
        "the A2K 0.1 envelope: a non-answer is a success whose data.outcome says so, and a "
        "failure is ok: false with error.code."
    ),
    # AgentCore Runtime hosts MCP servers at 0.0.0.0:8000/mcp (both are
    # FastMCP defaults, kept explicit here since Runtime depends on them).
    # stateless_http=True: this gateway has no sampling/elicitation/progress
    # notifications, so there's nothing that needs cross-request MCP session
    # state -- see README "Deploy to AgentCore".
    host="0.0.0.0",
    stateless_http=True,
)


def _dump(model) -> dict:
    return model.model_dump(mode="json")


@mcp.resource("a2k://card", mime_type="application/json")
def card() -> str:
    """This KB's A2K 0.1 KB Card."""
    return json.dumps(service_card(), ensure_ascii=False)


def cala_card() -> str:
    """Cala's (internal, pre-0.1) KB Card."""
    return load_card("cala").model_dump_json()


def sayari_card() -> str:
    """Sayari's (internal, pre-0.1) KB Card."""
    return load_card("sayari").model_dump_json()


async def a2k_list_vendors() -> dict:
    """Lists the vendor knowledge sources behind this gateway and what each one
    actually covers (domains, topics, human-readable scope, status, priority,
    queryType).

    Call this FIRST, before your first a2k.search/a2k.ask in a session --
    unconditionally, not just when it isn't obvious which vendor fits. Vendor
    coverage/status here is the current, authoritative answer; don't rely on
    a vendor's name, prior knowledge, or an earlier session to guess this
    instead. It's a cheap local lookup (no external API call), so there's no
    real cost to always calling it first.

    Then pass the matching `sourceId` value(s) as a2k.ask/a2k.search's
    `sources` param. Never select a vendor whose `status` isn't `active`.
    Omit `sources` entirely (fan out to all active vendors) when coverage
    genuinely overlaps or stays unclear even after checking this -- always a
    safe fallback, never wrong, just potentially less precise than a
    correctly scoped `sources`. `priority` (lower = preferred) is available
    as a tie-breaker if you need one, not a reason to skip fan-out when
    genuine ambiguity remains. `queryType` (`"entity"` or
    `"natural_language_query"`, may be null) tells you how to build the
    `query` you send *that specific vendor* -- see a2k.ask's own docstring
    for what each value means and why it differs per vendor; treat a null/
    unrecognized value as `"natural_language_query"` (send the question
    unmodified) rather than guessing. Same domains/topics/scope data as the
    a2k://card/<vendor> resources, exposed as a callable tool for clients
    that don't read MCP resources."""
    return {"vendors": vendor_catalogue()}


async def a2k_get_card() -> dict:
    """Returns this gateway's own KB Card (federates Cala + Sayari) --
    the same content as the `a2k://card` MCP resource and REST's
    `GET /.well-known/a2k-card.json`, exposed as a callable tool for
    clients that don't read MCP resources -- same reasoning as
    a2k.listVendors above for the per-vendor a2k://card/<vendor> resources.

    Call this to discover the gateway's own identity/capabilities (kbId,
    supported operations, security tier, etc. -- see A2K-KBCard-Schema)
    before your first request in a session, per A2K-KCP's prerequisite
    that a client holds a resolved KB Card before speaking the protocol
    (A2K-KCP-Consumption section 1)."""
    return _dump(load_card("gateway"))


async def _cala_raw_response_if_enabled(query: str, sources: list[str] | None) -> dict | None:
    """TEST-ONLY (config.cala_raw_knowledge_search, off by default): if enabled and
    Cala is among the requested sources (or none were specified), returns Cala's own
    knowledge_search `content` completely unprocessed instead of the normal cited
    envelope -- see Config.cala_raw_knowledge_search's docstring in config.py for why
    this exists. Returns None (meaning: proceed normally) when disabled, or when
    `sources` explicitly excludes Cala."""
    if not config.cala_raw_knowledge_search:
        return None
    if sources is not None and "cala" not in sources:
        return None
    cala_adapter = engine.adapters["cala"]
    return await cala_adapter.raw_knowledge_search(query)


async def a2k_search(query: str, sources: list[str] | None = None, limit: int = 10, onBehalfOf: str | None = None) -> dict:
    """Retrieve relevant passages with citations from Cala and/or Sayari. No synthesized answer -- use a2k.ask for that.

    Call a2k.listVendors first (unconditionally, once per session) if you
    haven't already, to know which `sourceId`(s) to pass as `sources` --
    don't guess from vendor names. Omitting `sources` fans out to all active
    vendors and is always safe, just potentially less precise.

    `query` should be the company/entity name itself, not a full sentence or
    question -- see a2k.ask's docstring for why.

    `onBehalfOf` -- the person asking (an id of the calling application's own
    users, e.g. the frontend's); recorded as userId in the audit record."""
    raw = await _cala_raw_response_if_enabled(query, sources)
    if raw is not None:
        return raw
    req = A2KRequest(operation="search", query=query, sources=sources, pagination={"limit": limit}, onBehalfOf=_on_behalf_of(onBehalfOf))
    return _dump(await engine.search(req))


# Transport headers a session id can be taken from when the caller sends none,
# in order of preference, with the prefix that marks where it came from.
_SESSION_HEADERS = (
    ("mcp-session-id", "mcp"),
    ("x-amzn-bedrock-agentcore-runtime-session-id", "agentcore"),
)


def _session_id(explicit: str | None, ctx: Context | None) -> str | None:
    """The caller's sessionId (K2) if given. Otherwise one derived from the
    transport -- the MCP session id or AgentCore's runtime session id --
    prefixed with its source ("mcp:", "agentcore:") so the serving log can
    tell the two apart. A derived session is only as good as the caller's
    connection handling: if one connection carries several end users, their
    consecutive questions look like one conversation. The cost is bounded --
    a reformulation can only deprecate an entry (a lost hit), never serve a
    wrong answer -- and the serving log's session_source lets the re-ask rate
    be computed on caller-provided sessions only."""
    if explicit:
        return explicit
    try:
        request = ctx.request_context.request if ctx is not None else None
    except (ValueError, AttributeError):  # no request in flight (e.g. called directly)
        request = None
    headers = getattr(request, "headers", None) or {}
    for header, source in _SESSION_HEADERS:
        value = headers.get(header)
        if value:
            return f"{source}:{value}"
    return None


async def a2k_ask(
    query: str,
    sources: list[str] | None = None,
    strictGrounding: bool = False,
    reportConflicts: bool = True,
    sessionId: str | None = None,
    onBehalfOf: str | None = None,
    ctx: Context | None = None,
) -> dict:
    """Ask a question about a company; returns a cited, synthesized answer.

    Call a2k.listVendors first (unconditionally, once per session) if you
    haven't already, to know which `sourceId`(s) to pass as `sources` --
    don't guess from vendor names. Omitting `sources` fans out to all active
    vendors and is always safe, just potentially less precise.

    `query` -- build it per vendor using that vendor's `queryType` from
    a2k.listVendors (`"entity"` or `"natural_language_query"`; treat a null/
    unrecognized value as `"natural_language_query"`):
    - `"entity"` -- extract just the company/entity name (e.g. "Allwyn
      Investments Cyprus") and send *only* that, not a full sentence or
      question about it (NOT "Allwyn Investments Cyprus detailed
      information" or "What do we know about Allwyn Investments Cyprus?").
      Confirmed for Sayari: its search (search_entities) is a name/text
      matcher only, no semantic fallback -- a query padded with extra words
      routinely returns zero matches even when the entity itself exists in
      Sayari's data, and there's no second attempt.
    - `"natural_language_query"` -- send the question as received, no
      extraction needed. Confirmed for Cala: if a strict name match finds
      nothing, it automatically falls back to a semantic knowledge-search
      tool that handles full questions fine.
    If `sources` spans vendors with *different* `queryType` values (or is
    omitted, fanning out to all active vendors), a single shared `query`
    can't be right for both -- call this tool once per vendor instead, each
    with a `query` built for that vendor's `queryType`, rather than one call
    with `sources` omitted. When in doubt or `sources` is omitted without
    checking per-vendor queryType first, use just the entity name -- it
    works for `natural_language_query` vendors too, just less richly than a
    fuller question would.

    `sources` optionally restricts to one provider, e.g. ["cala"] or
    ["sayari"]; omit it to fan out to both (default). If `conflicts` in the
    response is non-empty, the sources disagree on a fact -- surface both
    positions rather than picking one.

    `sessionId` -- the id of the end user's conversation, the same on every
    question of that conversation and different across users. Optional; when
    given, a question that rephrases the previous one within a few minutes is
    recognised as a re-ask and the answer it rephrases is not served again.
    Without it, the MCP/AgentCore session of the connection is used.

    `onBehalfOf` -- the person asking (an id of the calling application's own
    users, e.g. the frontend's); recorded as userId in the audit record and as
    `user` in the cache's serving log.
    """
    raw = await _cala_raw_response_if_enabled(query, sources)
    if raw is not None:
        return raw
    req = A2KRequest(
        operation="ask",
        query=query,
        sources=sources,
        requirements=Requirements(strictGrounding=strictGrounding, reportConflicts=reportConflicts),
        requestMetadata=RequestMetadata(sessionId=session_id) if (session_id := _session_id(sessionId, ctx)) else None,
        onBehalfOf=_on_behalf_of(onBehalfOf),
    )
    return _dump(await engine.ask(req))


def _on_behalf_of(user: str | None) -> OnBehalfOf | None:
    return OnBehalfOf(subject=user) if user else None


async def a2k_explain(answerRef: str, claimIds: list[str] | None = None) -> dict:
    """Explain a prior a2k.ask/a2k.search response, given its `audit.requestId` as answerRef."""
    req = ExplainRequest(answerRef=answerRef, claimIds=claimIds or [])
    return _dump(await engine.explain(req))


async def a2k_get_document(documentId: str) -> dict:
    """Retrieve the full source document/profile behind a citation's documentId."""
    req = GetDocumentRequest(documentId=documentId)
    return _dump(await engine.get_document(req))


async def a2k_validate_citation(documentId: str, exact: str) -> dict:
    """Level-4 verifiability helper: confirms `exact` text is actually present in the cited
    document, mitigating citation laundering (A2K-KCP section 19.7)."""
    req = GetDocumentRequest(documentId=documentId)
    response = await engine.get_document(req)
    if not response.ok or not response.document:
        message = response.error.message if response.error else "Document not found."
        return {"documentId": documentId, "valid": False, "sourceHash": None, "error": message}
    found = exact in (response.document.content or "")
    return {"documentId": documentId, "valid": found, "sourceHash": response.document.hash}


async def a2k_report_conflict(answerRef: str) -> dict:
    """Return the full cross-source ConflictReport for a prior a2k.ask response, if any."""
    envelope: CitedResponseEnvelope | None = engine.get_cached(answerRef)
    if envelope is None:
        return {"ok": False, "error": {"code": "NOT_FOUND", "message": f"No cached response for answerRef={answerRef!r}."}}
    if envelope.conflictReport is None:
        return {"ok": True, "conflictReport": None, "message": "No conflicts were detected for this response."}
    return {"ok": True, "conflictReport": _dump(envelope.conflictReport)}


async def a2k_get_audit_record(requestId: str) -> dict:
    """Look up the audit record for a given requestId in the append-only audit trail."""
    if not config.audit_log_path.exists():
        return {"found": False}
    with open(config.audit_log_path, encoding="utf-8") as fh:
        for line in fh:
            record = json.loads(line)
            if record.get("requestId") == requestId:
                return {"found": True, "audit": record}
    return {"found": False}


# --- A2K-KCP 0.1 tools ----------------------------------------------------------


def _arguments(**members: Any) -> dict[str, Any]:
    """The 0.1 request as sent: absent members stay absent (never null)."""
    return {k: v for k, v in members.items() if v is not None}


@mcp.tool(name="a2k.ask", structured_output=True)
async def kcp_ask(
    a2kVersion: str | None = None,
    operation: str | None = None,
    requestId: str | None = None,
    query: str | None = None,
    context: dict[str, Any] | None = None,
    onBehalfOf: dict[str, Any] | None = None,
    requirements: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """A2K-KCP 0.1 ask: a synthesized answer with claims and citations, or a
    substantive non-answer (data.outcome). Arguments are the 0.1 request:
    a2kVersion "0.1", operation "ask", requestId, query, and optionally
    context {purpose?, asOf?, riskLevel?}, requirements {features?,
    maxEvidenceAgeSeconds?}, filters."""
    arguments = _arguments(
        a2kVersion=a2kVersion, operation=operation, requestId=requestId, query=query,
        context=context, onBehalfOf=onBehalfOf, requirements=requirements, filters=filters,
    )
    return await kcp01.ask(arguments, session_id=_session_id(None, ctx))


@mcp.tool(name="a2k.search", structured_output=True)
async def kcp_search(
    a2kVersion: str | None = None,
    operation: str | None = None,
    requestId: str | None = None,
    query: str | None = None,
    context: dict[str, Any] | None = None,
    onBehalfOf: dict[str, Any] | None = None,
    requirements: dict[str, Any] | None = None,
    filters: dict[str, Any] | None = None,
    pagination: dict[str, Any] | None = None,
    ctx: Context | None = None,
) -> dict[str, Any]:
    """A2K-KCP 0.1 search: passages with citations, no synthesis. Arguments
    are the 0.1 request: a2kVersion "0.1", operation "search", requestId,
    query, and optionally context, requirements {maxEvidenceAgeSeconds?},
    filters and pagination {limit?} (a single page: no cursor)."""
    arguments = _arguments(
        a2kVersion=a2kVersion, operation=operation, requestId=requestId, query=query, context=context,
        onBehalfOf=onBehalfOf, requirements=requirements, filters=filters, pagination=pagination,
    )
    return await kcp01.search(arguments, session_id=_session_id(None, ctx))


def _declare_output_schemas() -> None:
    """outputSchema = the reference package's self-contained response bundle
    for each operation (section 10.2). FastMCP has no parameter for a given
    schema, so it replaces the one it derived from the return annotation.
    The inputSchema it derived is kept, closed and with the members the 0.1
    request requires (the arguments themselves are judged by the reference
    models, see _KcpFastMCP)."""
    from a2k_kcp.schema import load_bundle

    for name, operation in (("a2k.ask", "ask"), ("a2k.search", "search")):
        tool = mcp._tool_manager.get_tool(name)
        tool.fn_metadata.output_schema = load_bundle(operation)
        tool.parameters["additionalProperties"] = False
        tool.parameters["required"] = ["a2kVersion", "operation", "requestId", "query"]


_declare_output_schemas()


# --- internal (pre-0.1) tools, A2K_INTERNAL_TOOLS=true only ---------------------

_INTERNAL_TOOLS = {
    "internal.ask": a2k_ask,
    "internal.search": a2k_search,
    "internal.listVendors": a2k_list_vendors,
    "internal.getCard": a2k_get_card,
    "internal.explain": a2k_explain,
    "internal.getDocument": a2k_get_document,
    "internal.validateCitation": a2k_validate_citation,
    "internal.reportConflict": a2k_report_conflict,
    "internal.getAuditRecord": a2k_get_audit_record,
}


def register_internal_tools() -> None:
    for name, fn in _INTERNAL_TOOLS.items():
        mcp.tool(name=name)(fn)
    mcp.resource("internal://card/cala")(cala_card)
    mcp.resource("internal://card/sayari")(sayari_card)


if config.internal_tools:
    register_internal_tools()


class _TrailingSlashAliasMiddleware:
    """AgentCore Runtime's own docs disagree with each other on whether its
    internal sidecar (liveness probe *and*, per one troubleshooting page,
    real MCP traffic too) targets `/mcp` or `/mcp/` -- both spellings appear
    across AWS's own pages. FastMCP registers the endpoint at `/mcp` only
    (exact match, no redirect), so anything arriving at `/mcp/` 404s before
    it reaches real handling. Rather than guess which exact requests need
    special-casing (see git history for an earlier, narrower attempt that
    only intercepted the probe's specific fingerprint and still failed),
    this rewrites the ASGI scope's path unconditionally before routing, so
    `/mcp` and `/mcp/` are indistinguishable to everything downstream --
    correct for the probe, for real invocations, and for a human hitting
    either path with curl.
    """

    def __init__(self, app):
        self._app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http" and scope.get("path") == "/mcp/":
            scope = dict(scope)
            scope["path"] = "/mcp"
        await self._app(scope, receive, send)


async def _ping(request):
    """AgentCore Runtime's generic health/idle-tracking contract (separate
    from the MCP-specific /mcp probe): a GET /ping the platform polls to
    read `status` (Healthy/HealthyBusy) for idle-session accounting. FastMCP
    doesn't register this on its own -- added explicitly since AgentCore
    Runtime's own troubleshooting docs list a missing /ping among the
    startup/health failure causes.

    "HealthyBusy" while a cache admission is still running in the background
    (a2k/cache/admission.py): the answer has already been returned, so to the
    platform the session looks idle and could be reclaimed mid-admission.
    Otherwise "Healthy" -- idle, safe to reclaim after the idle timeout."""
    return JSONResponse({"status": "HealthyBusy" if admission.in_flight() else "Healthy"})


def main() -> None:
    # Same steps FastMCP.run(transport="streamable-http") takes internally
    # (see mcp.server.fastmcp.FastMCP.run_streamable_http_async) -- done by
    # hand here to add the /mcp//mcp alias and the /ping route, neither of
    # which FastMCP has a constructor hook for.
    async def _run() -> None:
        app = mcp.streamable_http_app()
        app.routes.append(Route("/ping", _ping, methods=["GET"]))
        app = _TrailingSlashAliasMiddleware(app)
        server_config = uvicorn.Config(
            app,
            host=mcp.settings.host,
            port=mcp.settings.port,
            log_level=mcp.settings.log_level.lower(),
        )
        await uvicorn.Server(server_config).serve()

    anyio.run(_run)


if __name__ == "__main__":
    main()
