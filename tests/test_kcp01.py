"""A2K-KCP 0.1 conformance of the surface K2 calls (a2k/kcp01/, the a2k.ask
and a2k.search MCP tools): every envelope produced here is validated against
the reference package's JSON Schemas, and the reference corpus of invalid
requests must come back as failures.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from a2k_kbcard.schema.validation import validate_by_name

from a2k.config import config
from a2k.gateway.engine import engine
from a2k.kcp01 import adapter
from a2k.kcp01.card import service_card
from a2k.mcp_server.server import mcp
from a2k.models.envelope import Citation, CitedResponseEnvelope, ErrorObject, Passage

CORPUS = Path(__file__).resolve().parents[2] / "sgt-ap11702-a2k-main" / "tests" / "corpus" / "negative"
URL = "https://www.eba.europa.eu/guidelines/internal-governance"


def ask_request(**overrides) -> dict:
    return {"a2kVersion": "0.1", "operation": "ask", "requestId": "req-1", "query": "What does the EBA require?", **overrides}


def search_request(**overrides) -> dict:
    return {"a2kVersion": "0.1", "operation": "search", "requestId": "req-2", "query": "EBA internal governance", **overrides}


def internal(answer="Según la EBA, el órgano de administración debe promover la cultura de riesgos.", citations=None, **kw) -> CitedResponseEnvelope:
    if citations is None:
        citations = [Citation(id="citation-1", documentId="doc-1", title="EBA GL", sourceUrl=URL, retrievedAt="2026-10-08T09:00:00Z")]
    return CitedResponseEnvelope(ok=True, operation=kw.pop("operation", "ask"), sourceKbId="urn:a2k:internal:x", answer=answer, citations=citations, **kw)


@pytest.fixture
def engine_returns(monkeypatch):
    """Makes engine.ask / engine.search return a given internal envelope and records the request."""
    seen = {}

    def set_(envelope: CitedResponseEnvelope):
        async def fake(req):
            seen["req"] = req
            return envelope

        monkeypatch.setattr(engine, "ask", fake)
        monkeypatch.setattr(engine, "search", fake)
        return seen

    return set_


async def _call(tool: str, arguments: dict) -> dict:
    _, structured = await mcp.call_tool(tool, arguments)
    return structured


# --- card and MCP surface ------------------------------------------------------


def test_service_card_is_a_valid_0_1_kb_card():
    card = service_card()
    validate_by_name(card, "kb-card")
    assert card["operations"] == ["search", "ask"]
    assert "cala" not in json.dumps(card).lower() and "sayari" not in json.dumps(card).lower()


async def test_tools_declare_the_reference_output_schemas_and_the_card_resource():
    tools = {t.name: t for t in await mcp.list_tools()}
    assert tools["a2k.ask"].outputSchema["title"] == "A2K 0.1 ask response"
    assert tools["a2k.search"].outputSchema["title"] == "A2K 0.1 search response"
    (content,) = await mcp.read_resource("a2k://card")
    assert json.loads(content.content)["id"] == service_card()["id"]


# --- ask ---------------------------------------------------------------------------


async def test_ask_answered_is_one_claim_spanning_the_answer(engine_returns):
    seen = engine_returns(internal())
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    assert out["ok"] is True and out["requestId"] == "req-1" and out["kbId"] == service_card()["id"]
    data = out["data"]
    assert data["outcome"] == "answered"
    (claim,) = data["claims"]
    assert claim["status"] == "SUPPORTED" and claim["citationIds"] == ["citation-1"]
    assert claim["answerSpans"] == [{"start": 0, "end": len(data["answer"])}]  # code points: "ó" is one
    assert data["grounding"] == {"status": "supported", "method": "llm-self-report"}
    assert data["citations"][0]["sourceUrl"] == URL
    assert seen["req"].requestMetadata.requestId == "req-1"


async def test_ask_without_usable_citations_is_answered_but_unsupported(engine_returns):
    engine_returns(internal(citations=[Citation(id="citation-1", documentId="api-result", retrievedAt="2026-10-08T09:00:00Z")]))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    assert out["data"]["citations"] == [] and out["data"]["claims"][0]["status"] == "INSUFFICIENT_EVIDENCE"
    assert out["data"]["grounding"]["status"] == "unsupported"


async def test_ask_with_nothing_found_is_a_refusal_not_an_error(engine_returns):
    engine_returns(internal(answer=None, citations=[]))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    assert out["ok"] is True and out["data"]["outcome"] == "refused"
    assert out["data"]["refusal"]["code"] == "INSUFFICIENT_EVIDENCE" and "answer" not in out["data"]


async def test_vendor_unavailable_is_a_retryable_upstream_error(engine_returns):
    engine_returns(internal(answer=config.vendor_fallback_message))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    assert out["ok"] is False and out["error"]["code"] == "UPSTREAM_ERROR" and out["error"]["retryable"] is True


async def test_internal_errors_are_mapped_with_caller_safe_messages(engine_returns):
    engine_returns(CitedResponseEnvelope(ok=False, operation="ask", sourceKbId="x", error=ErrorObject(code="TIMEOUT", message="Agent Runtime call to arn:aws:... timed out")))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    assert out["error"]["code"] == "TIMEOUT" and "arn:" not in out["error"]["message"]
    engine_returns(CitedResponseEnvelope(ok=False, operation="ask", sourceKbId="x", error=ErrorObject(code="OUT_OF_SCOPE", message="x")))
    out = await _call("a2k.ask", ask_request())
    assert out["ok"] is True and out["data"]["outcome"] == "out-of-scope" and out["data"]["refusal"]["code"] == "OUT_OF_SCOPE"


async def test_conflicts_become_warnings(engine_returns):
    from a2k.models.envelope import AwareConflict, AwareConflictSource

    conflict = AwareConflict(id="x1", claimId="c1", nature="value-conflict", thisPosition="CET1 15.2%", otherPosition="CET1 15.4%",
                             otherSource=AwareConflictSource(kbId="urn:a2k:vendor:sayari"), assessment="a", rationale="r")
    engine_returns(internal(conflicts=[conflict]))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    assert out["data"]["warnings"] == ["The sources disagree: CET1 15.2% / CET1 15.4%"]


@pytest.mark.parametrize(
    "overrides, outcome, code",
    [
        ({"context": {"asOf": "2024-06-01T00:00:00Z"}}, "refused", "POINT_IN_TIME_UNSUPPORTED"),
        ({"context": {"purpose": "model-training"}}, "refused", "PURPOSE_NOT_ALLOWED"),
        ({"filters": {"maxClassificationLevel": 0}}, "refused", "INSUFFICIENT_EVIDENCE"),
        # No sourceVerifiedAt anywhere: no evidence can meet any maximum age (section 5.5).
        ({"requirements": {"maxEvidenceAgeSeconds": 31536000}}, "refused", "EVIDENCE_TOO_OLD"),
    ],
)
async def test_ask_refusals_settled_before_answering(engine_returns, overrides, outcome, code):
    seen = engine_returns(internal())
    out = await _call("a2k.ask", ask_request(**overrides))
    validate_by_name(out, "ask-response")
    assert out["data"]["outcome"] == outcome and out["data"]["refusal"]["code"] == code
    assert "req" not in seen  # the engine was never called


async def test_strict_grounding_is_an_unsupported_required_feature(engine_returns):
    seen = engine_returns(internal())
    out = await _call("a2k.ask", ask_request(requirements={"features": ["strict-grounding"]}))
    validate_by_name(out, "ask-response")
    assert out["ok"] is False and out["error"]["code"] == "REQUIRED_FEATURE_UNSUPPORTED" and "req" not in seen


@pytest.mark.parametrize(
    "arguments, code, echoes_request_id",
    [
        (ask_request(a2kVersion="0.6-baseline"), "UNSUPPORTED_VERSION", True),
        (ask_request(operation="search"), "INVALID_REQUEST", True),
        ({k: v for k, v in ask_request().items() if k != "requestId"}, "SCHEMA_VALIDATION_FAILED", False),
        ({k: v for k, v in ask_request().items() if k != "a2kVersion"}, "SCHEMA_VALIDATION_FAILED", True),
        (ask_request(context={"purpose": "not-a-purpose"}), "SCHEMA_VALIDATION_FAILED", True),
    ],
)
async def test_invalid_ask_requests_are_failure_envelopes(arguments, code, echoes_request_id):
    out = await _call("a2k.ask", arguments)
    validate_by_name(out, "ask-response")
    assert out["ok"] is False and out["error"]["code"] == code
    assert ("requestId" in out) is echoes_request_id


# --- search --------------------------------------------------------------------------


async def test_search_passages_keep_only_usable_citations(engine_returns):
    citations = [
        Citation(id="citation-1", documentId="doc-1", sourceUrl=URL, retrievedAt="2026-10-08T09:00:00Z"),
        Citation(id="citation-2", documentId="api", retrievedAt="2026-10-08T09:00:00Z"),
    ]
    passages = [
        Passage(id="p1", text="The management body should promote a sound risk culture.", citationIds=["citation-1"], score=0.8),
        Passage(id="p2", text="No locator for this one.", citationIds=["citation-2"]),
    ]
    seen = engine_returns(internal(operation="search", answer=None, citations=citations, passages=passages))
    out = await _call("a2k.search", search_request(pagination={"limit": 5}))
    validate_by_name(out, "search-response")
    data = out["data"]
    assert [p["id"] for p in data["passages"]] == ["p1"] and [c["id"] for c in data["citations"]] == ["citation-1"]
    assert data["pageInfo"] == {"nextCursor": None}
    assert seen["req"].pagination.limit == 5


@pytest.mark.parametrize(
    "overrides, code",
    [
        ({"pagination": {"cursor": "abc"}}, "PAGINATION_CURSOR_INVALID"),
        ({"context": {"asOf": "2024-06-01T00:00:00Z"}}, "INVALID_REQUEST"),
        ({"context": {"purpose": "bulk-export"}}, "ACCESS_DENIED"),
    ],
)
async def test_search_failures_settled_before_searching(engine_returns, overrides, code):
    seen = engine_returns(internal(operation="search"))
    out = await _call("a2k.search", search_request(**overrides))
    validate_by_name(out, "search-response")
    assert out["ok"] is False and out["error"]["code"] == code and "req" not in seen


# --- the reference corpus of invalid requests ------------------------------------


@pytest.mark.skipif(not CORPUS.exists(), reason="reference repo (sgt-ap11702-a2k-main) not next to this one")
@pytest.mark.parametrize("operation", ["ask", "search"])
async def test_reference_negative_requests_are_all_rejected(engine_returns, operation):
    engine_returns(internal(operation=operation))
    handler = adapter.ask if operation == "ask" else adapter.search
    files = sorted((CORPUS / f"{operation}-request").glob("*.json"))
    assert files
    for path in files:
        out = await handler(json.loads(path.read_text(encoding="utf-8")))
        validate_by_name(out, f"{operation}-response")
        # Rejected: a failure, or (ask) a refusal settled before answering.
        assert out["ok"] is False or out["data"].get("outcome") not in ("answered", None), path.name


async def test_end_to_end_over_an_mcp_client_session(engine_returns):
    """Through the real MCP protocol (client <-> server in memory): the
    structuredContent is the 0.1 envelope and passes the server's own
    outputSchema check; a failure envelope is a normal result, isError false."""
    from mcp.shared.memory import create_connected_server_and_client_session

    engine_returns(internal())
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        ok = await client.call_tool("a2k.ask", ask_request())
        assert ok.isError is False and ok.structuredContent["data"]["outcome"] == "answered"
        validate_by_name(ok.structuredContent, "ask-response")
        failed = await client.call_tool("a2k.ask", ask_request(a2kVersion="0.2"))
        assert failed.isError is False and failed.structuredContent["error"]["code"] == "UNSUPPORTED_VERSION"


async def test_search_with_a_maximum_evidence_age_is_empty_with_a_warning(engine_returns):
    seen = engine_returns(internal(operation="search"))
    out = await _call("a2k.search", search_request(requirements={"maxEvidenceAgeSeconds": 86400}))
    validate_by_name(out, "search-response")
    assert out["ok"] is True and out["data"]["passages"] == [] and out["data"]["warnings"] and "req" not in seen


async def test_citations_to_a_vendor_api_are_not_sources(engine_returns):
    citations = [
        Citation(id="citation-1", documentId="doc-1", sourceUrl=URL, retrievedAt="2026-10-08T09:00:00Z"),
        Citation(id="citation-2", documentId="e1", sourceUrl="https://api.cala.ai/v1/entities/ccca3f91", retrievedAt="2026-10-08T09:00:00Z"),
    ]
    engine_returns(internal(citations=citations))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    assert [c["id"] for c in out["data"]["citations"]] == ["citation-1"]


async def test_unknown_or_null_members_are_rejected_through_mcp(engine_returns):
    seen = engine_returns(internal())
    out = await _call("a2k.ask", ask_request(sources=["cala"]))
    validate_by_name(out, "ask-response")
    assert out["ok"] is False and out["error"]["code"] == "SCHEMA_VALIDATION_FAILED" and "req" not in seen
    out = await _call("a2k.ask", ask_request(context=None))
    assert out["error"]["code"] == "SCHEMA_VALIDATION_FAILED"


async def test_input_schema_is_closed_with_the_required_members():
    tools = {t.name: t for t in await mcp.list_tools()}
    schema = tools["a2k.ask"].inputSchema
    assert schema["additionalProperties"] is False
    assert set(schema["required"]) == {"a2kVersion", "operation", "requestId", "query"}


@pytest.mark.skipif(not CORPUS.exists(), reason="reference repo (sgt-ap11702-a2k-main) not next to this one")
@pytest.mark.parametrize("operation", ["ask", "search"])
async def test_reference_negative_requests_are_rejected_through_mcp(engine_returns, operation):
    from mcp.shared.memory import create_connected_server_and_client_session

    engine_returns(internal(operation=operation))
    async with create_connected_server_and_client_session(mcp._mcp_server) as client:
        for path in sorted((CORPUS / f"{operation}-request").glob("*.json")):
            result = await client.call_tool(f"a2k.{operation}", json.loads(path.read_text(encoding="utf-8")))
            out = result.structuredContent
            assert result.isError is False, path.name
            validate_by_name(out, f"{operation}-response")
            assert out["ok"] is False or out["data"].get("outcome") not in ("answered", None), path.name


async def test_search_with_every_vendor_unavailable_is_an_upstream_error(monkeypatch):
    from a2k.gateway.engine import GatewayEngine
    from a2k.models.request import A2KRequest

    async def unavailable(self, operation, query, sources, request_id):
        return {"passages": [], "citations": [], "usage": {}, "toolCalls": [], "vendorUnavailable": True}

    monkeypatch.setattr(GatewayEngine, "_call_agent", unavailable)
    env = await GatewayEngine()._search_via_agent(A2KRequest(operation="search", query="q"))
    out = adapter.search_response(adapter.SearchRequest.model_validate(search_request()), env)
    validate_by_name(out, "search-response")
    assert out["ok"] is False and out["error"]["code"] == "UPSTREAM_ERROR" and out["error"]["retryable"] is True


# --- claims located in the answer, coverage -> outcome ------------------------------

ANSWER = "La EBA exige un órgano de administración responsable 📈. El ratio CET1 es del 15,2 € por cada 100.\nFin."


def _with_claims(*claims, coverage="complete", missing=(), citations=None):
    from a2k.models.envelope import Claim

    if citations is None:
        citations = [
            Citation(id="citation-1", documentId="d1", sourceUrl=URL, retrievedAt="2026-10-08T09:00:00Z"),
            Citation(id="citation-2", documentId="d2", sourceUrl="https://www.ecb.europa.eu/stats", retrievedAt="2026-10-08T09:00:00Z"),
            Citation(id="citation-3", documentId="api", retrievedAt="2026-10-08T09:00:00Z"),  # no URL: not usable
        ]
    claim_objs = [Claim(id=f"claim-{i}", text=t, status="SUPPORTED", citationIds=ids) for i, (t, ids) in enumerate(claims, 1)]
    return internal(answer=ANSWER, citations=citations, claims=claim_objs, coverage=coverage, missing=list(missing))


async def test_claims_are_located_with_code_point_spans(engine_returns):
    engine_returns(_with_claims(
        ("La EBA exige un órgano de administración responsable 📈.", ["citation-1"]),
        ("El ratio CET1 es del   15,2 € por cada 100.", ["citation-2"]),  # extra spaces: still located
        ("A sentence the answer never says.", ["citation-1"]),            # not in the answer: dropped
    ))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    data = out["data"]
    first, second = data["claims"]
    s1, s2 = first["answerSpans"][0], second["answerSpans"][0]
    assert data["answer"][s1["start"]:s1["end"]] == "La EBA exige un órgano de administración responsable 📈."
    assert data["answer"][s2["start"]:s2["end"]] == "El ratio CET1 es del 15,2 € por cada 100."
    assert first["citationIds"] == ["citation-1"] and second["citationIds"] == ["citation-2"]
    assert data["grounding"]["status"] == "supported" and data["outcome"] == "answered"
    assert {c["id"] for c in data["citations"]} == {"citation-1", "citation-2"}


async def test_a_claim_citing_only_unusable_sources_makes_grounding_partial(engine_returns):
    engine_returns(_with_claims(
        ("La EBA exige un órgano de administración responsable 📈.", ["citation-1"]),
        ("El ratio CET1 es del 15,2 € por cada 100.", ["citation-3"]),
    ))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    assert [c["status"] for c in out["data"]["claims"]] == ["SUPPORTED", "INSUFFICIENT_EVIDENCE"]
    assert out["data"]["grounding"]["status"] == "partial"


async def test_no_locatable_claim_falls_back_to_one_claim_over_the_answer(engine_returns):
    engine_returns(_with_claims(("Paraphrased, not in the answer.", ["citation-1"])))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    (claim,) = out["data"]["claims"]
    assert claim["answerSpans"] == [{"start": 0, "end": len(ANSWER)}]


async def test_partial_coverage_is_outcome_partial_with_what_is_missing(engine_returns):
    engine_returns(_with_claims(("La EBA exige un órgano de administración responsable 📈.", ["citation-1"]),
                                coverage="partial", missing=["Q4 2023 CET1 ratio"]))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    data = out["data"]
    assert data["outcome"] == "partial" and data["answer"] == ANSWER
    assert data["refusal"]["code"] == "INSUFFICIENT_EVIDENCE" and data["warnings"] == ["Not answered: Q4 2023 CET1 ratio"]


async def test_coverage_none_is_a_refusal_even_with_answer_text(engine_returns):
    engine_returns(_with_claims(coverage="none"))
    out = await _call("a2k.ask", ask_request())
    validate_by_name(out, "ask-response")
    assert out["data"]["outcome"] == "refused" and "answer" not in out["data"]


def test_claims_are_located_ignoring_markdown_emphasis():
    from a2k.kcp01.adapter import _locate

    answer = "Cartera:\n\n- **Q4 2022:** $9.9 billion (net of provisions)\n- **AIR (AI by Revolut)** - Launched on April 9, 2026."
    for claim in ("Q4 2022: $9.9 billion (net of provisions)", "AIR (AI by Revolut) - Launched on April 9, 2026."):
        start, end = _locate(claim, answer)
        assert answer[start:end].replace("*", "") == claim
    assert _locate("Not in the answer at all", answer) is None
