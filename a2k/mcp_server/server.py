"""MCP transport binding (A2K-KCP-Consumption 4.md, section 2).

Same shape as `api/rest.py`: every tool here parses arguments into a request
model, calls `gateway/engine.py`, and returns the resulting envelope as a
dict. No business logic lives in this module -- K2 gets an identical
envelope whether it talks to the box over REST or over this MCP server.

Run standalone with `python -m a2k.mcp_server` (stdio transport).
"""

from __future__ import annotations

import json

from mcp.server.fastmcp import FastMCP

from ..cards import load_card
from ..config import config
from ..gateway.engine import engine
from ..models.envelope import CitedResponseEnvelope
from ..models.request import A2KRequest, ExplainRequest, GetDocumentRequest, Requirements

mcp = FastMCP(
    name="a2k-box",
    instructions=(
        "A2K-KCP gateway fronting Cala (financial/legal/regulatory filings) and "
        "Sayari (ownership/risk graph) company-intelligence data for the K2 agent. "
        "Call a2k.search for raw cited passages, or a2k.ask for a synthesized, cited "
        "answer. If the response's `conflicts` array is non-empty, Cala and Sayari "
        "disagree on a fact -- surface both positions to the user; never silently "
        "prefer one source. Use a2k.explain(answerRef=<audit.requestId>) to get the "
        "evidence behind a prior a2k.ask response, and a2k.getDocument to retrieve "
        "the full source record behind any citation's documentId."
    ),
)


def _dump(model) -> dict:
    return model.model_dump(mode="json")


@mcp.resource("a2k://card")
def gateway_card() -> str:
    """The gateway's own KB Card (federates Cala + Sayari)."""
    return load_card("gateway").model_dump_json()


@mcp.resource("a2k://card/cala")
def cala_card() -> str:
    """Cala's KB Card."""
    return load_card("cala").model_dump_json()


@mcp.resource("a2k://card/sayari")
def sayari_card() -> str:
    """Sayari's KB Card."""
    return load_card("sayari").model_dump_json()


@mcp.tool(name="a2k.search")
async def a2k_search(query: str, sources: list[str] | None = None, limit: int = 10) -> dict:
    """Retrieve relevant passages with citations from Cala and/or Sayari. No synthesized answer -- use a2k.ask for that."""
    req = A2KRequest(operation="search", query=query, sources=sources, pagination={"limit": limit})
    return _dump(await engine.search(req))


@mcp.tool(name="a2k.ask")
async def a2k_ask(
    query: str,
    sources: list[str] | None = None,
    strictGrounding: bool = False,
    reportConflicts: bool = True,
) -> dict:
    """Ask a question about a company; returns a cited, synthesized answer.

    `sources` optionally restricts to one provider, e.g. ["cala"] or
    ["sayari"]; omit it to fan out to both (default). If `conflicts` in the
    response is non-empty, the sources disagree on a fact -- surface both
    positions rather than picking one.
    """
    req = A2KRequest(
        operation="ask",
        query=query,
        sources=sources,
        requirements=Requirements(strictGrounding=strictGrounding, reportConflicts=reportConflicts),
    )
    return _dump(await engine.ask(req))


@mcp.tool(name="a2k.explain")
async def a2k_explain(answerRef: str, claimIds: list[str] | None = None) -> dict:
    """Explain a prior a2k.ask/a2k.search response, given its `audit.requestId` as answerRef."""
    req = ExplainRequest(answerRef=answerRef, claimIds=claimIds or [])
    return _dump(await engine.explain(req))


@mcp.tool(name="a2k.getDocument")
async def a2k_get_document(documentId: str) -> dict:
    """Retrieve the full source document/profile behind a citation's documentId."""
    req = GetDocumentRequest(documentId=documentId)
    return _dump(await engine.get_document(req))


@mcp.tool(name="a2k.validateCitation")
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


@mcp.tool(name="a2k.reportConflict")
async def a2k_report_conflict(answerRef: str) -> dict:
    """Return the full cross-source ConflictReport for a prior a2k.ask response, if any."""
    envelope: CitedResponseEnvelope | None = engine.get_cached(answerRef)
    if envelope is None:
        return {"ok": False, "error": {"code": "NOT_FOUND", "message": f"No cached response for answerRef={answerRef!r}."}}
    if envelope.conflictReport is None:
        return {"ok": True, "conflictReport": None, "message": "No conflicts were detected for this response."}
    return {"ok": True, "conflictReport": _dump(envelope.conflictReport)}


@mcp.tool(name="a2k.getAuditRecord")
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


def main() -> None:
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
