"""Tests for gateway/engine.py's live-mode, agent-mediated path
(_ask_via_agent/_search_via_agent -- the mcp_to_agent_to_mcp branch's actual
change). The rest of this suite only ever exercises the mock-mode
deterministic path (search()/ask() dispatch on config.is_mock, see
engine.py) -- this file is what closes that gap for the new code.

_call_agent() is monkeypatched directly rather than faking the underlying
HTTPS call: reaching the real agent Runtime needs live Cognito, Bedrock, and
a deployed agent none of this test suite has (that round trip can only be
verified by actually deploying, see the branch's plan). What *is* fully
testable without any of that, and what these tests cover, is everything
this gateway itself does with whatever the agent returns: building real
Claim/Citation/Passage/AwareConflict objects with correct IDs from the
agent's index-based citationIndexes/thisClaimIndex/otherClaimIndex, the same
insufficient-evidence and strict-grounding rules the deterministic path
enforces, and clean error-envelope construction when the agent call fails.
"""

import pytest

from a2k.errors import A2KError, ErrorCode
from a2k.gateway.engine import GatewayEngine
from a2k.models.request import A2KRequest, Pagination, Requirements


@pytest.fixture
def engine():
    return GatewayEngine()


async def test_ask_via_agent_maps_claims_and_citations(engine, monkeypatch):
    async def fake_call_agent(self, operation, query, sources, request_id):
        assert operation == "ask"
        return {
            "answer": "Acme Robotics is active.",
            "claims": [{"text": "Acme Robotics is active.", "status": "SUPPORTED", "citationIndexes": [0]}],
            "citations": [
                {"title": "Acme Robotics filing", "sourceUrl": "https://example.com/acme", "documentId": "doc-1"}
            ],
            "groundedRatio": 0.95,
            "conflicts": [],
        }

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)

    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="Acme Robotics"))

    assert envelope.ok is True
    assert envelope.answer == "Acme Robotics is active."
    assert len(envelope.claims) == 1
    assert envelope.claims[0].id == "claim-1"
    assert envelope.claims[0].citationIds == ["citation-1"]
    assert envelope.citations[0].id == "citation-1"
    assert envelope.citations[0].sourceUrl == "https://example.com/acme"
    assert envelope.grounding.groundedRatio == 0.95
    assert envelope.grounding.confidenceMethod == "llm-self-report"


async def test_ask_via_agent_maps_conflicts(engine, monkeypatch):
    async def fake_call_agent(self, operation, query, sources, request_id):
        return {
            "answer": "Ownership is disputed.",
            "claims": [
                {"text": "Cala says 62% owned by X.", "status": "DISPUTED", "citationIndexes": [0]},
                {"text": "Sayari says 48% owned by X.", "status": "DISPUTED", "citationIndexes": [1]},
            ],
            "citations": [
                {"title": "Cala filing", "sourceUrl": "https://cala.example/1"},
                {"title": "Sayari record", "sourceUrl": "https://sayari.example/1"},
            ],
            "groundedRatio": 0.9,
            "conflicts": [
                {
                    "thisClaimIndex": 0,
                    "otherClaimIndex": 1,
                    "nature": "value-conflict",
                    "assessment": "Sources disagree on ownership percentage.",
                    "rationale": "62% vs 48%.",
                }
            ],
        }

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)

    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="X ownership"))

    assert len(envelope.conflicts) == 1
    conflict = envelope.conflicts[0]
    assert conflict.nature == "value-conflict"
    assert conflict.claimId == "claim-1"
    assert "62%" in conflict.thisPosition
    assert "48%" in conflict.otherPosition


async def test_ask_via_agent_unrecognized_conflict_nature_falls_back_to_unknown(engine, monkeypatch):
    async def fake_call_agent(self, operation, query, sources, request_id):
        return {
            "answer": "x",
            "claims": [
                {"text": "a", "status": "DISPUTED", "citationIndexes": []},
                {"text": "b", "status": "DISPUTED", "citationIndexes": []},
            ],
            "citations": [],
            "groundedRatio": 0.5,
            "conflicts": [
                {
                    "thisClaimIndex": 0,
                    "otherClaimIndex": 1,
                    "nature": "not-a-real-conflict-type",
                    "assessment": "a",
                    "rationale": "b",
                }
            ],
        }

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="x"))
    assert envelope.conflicts[0].nature == "unknown"


async def test_ask_via_agent_out_of_range_conflict_index_is_dropped(engine, monkeypatch):
    async def fake_call_agent(self, operation, query, sources, request_id):
        return {
            "answer": "x",
            "claims": [{"text": "a", "status": "SUPPORTED", "citationIndexes": []}],
            "citations": [],
            "groundedRatio": 1.0,
            "conflicts": [
                {"thisClaimIndex": 0, "otherClaimIndex": 5, "nature": "unknown", "assessment": "", "rationale": ""}
            ],
        }

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="x"))
    assert envelope.conflicts == []


async def test_ask_via_agent_no_claims_no_answer_is_insufficient_evidence(engine, monkeypatch):
    async def fake_call_agent(self, operation, query, sources, request_id):
        return {"answer": None, "claims": [], "citations": [], "groundedRatio": 0.0, "conflicts": []}

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="Totally Unknown Company XYZ"))
    assert envelope.ok is True
    assert envelope.claims[0].status == "INSUFFICIENT_EVIDENCE"


async def test_ask_via_agent_strict_grounding_violation_when_ratio_below_one(engine, monkeypatch):
    async def fake_call_agent(self, operation, query, sources, request_id):
        return {
            "answer": "Partial answer.",
            "claims": [{"text": "a", "status": "SUPPORTED", "citationIndexes": []}],
            "citations": [],
            "groundedRatio": 0.7,
            "conflicts": [],
        }

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    req = A2KRequest(operation="ask", query="x", requirements=Requirements(strictGrounding=True))
    envelope = await engine._ask_via_agent(req)
    assert envelope.ok is False
    assert envelope.error.code == ErrorCode.GROUNDING_VIOLATION


async def test_ask_via_agent_strict_grounding_satisfied_at_exactly_one(engine, monkeypatch):
    async def fake_call_agent(self, operation, query, sources, request_id):
        return {
            "answer": "Full answer.",
            "claims": [{"text": "a", "status": "SUPPORTED", "citationIndexes": []}],
            "citations": [],
            "groundedRatio": 1.0,
            "conflicts": [],
        }

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    req = A2KRequest(operation="ask", query="x", requirements=Requirements(strictGrounding=True))
    envelope = await engine._ask_via_agent(req)
    assert envelope.ok is True
    assert envelope.grounding.strictGroundingSatisfied is True


async def test_ask_via_agent_propagates_call_agent_failure_as_error_envelope(engine, monkeypatch):
    async def fake_call_agent(self, operation, query, sources, request_id):
        raise A2KError(ErrorCode.UPSTREAM_ERROR, "boom")

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    envelope = await engine._ask_via_agent(A2KRequest(operation="ask", query="x"))
    assert envelope.ok is False
    assert "boom" in envelope.error.message


async def test_search_via_agent_maps_passages_and_citations(engine, monkeypatch):
    async def fake_call_agent(self, operation, query, sources, request_id):
        assert operation == "search"
        return {
            "passages": [{"text": "Acme Robotics filed a 10-K.", "citationIndexes": [0]}],
            "citations": [{"title": "Acme 10-K", "sourceUrl": "https://example.com/10k"}],
        }

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    envelope = await engine._search_via_agent(A2KRequest(operation="search", query="Acme Robotics"))

    assert envelope.ok is True
    assert envelope.answer is None
    assert len(envelope.passages) == 1
    assert envelope.passages[0].citationIds == ["citation-1"]


async def test_search_via_agent_respects_pagination_limit(engine, monkeypatch):
    async def fake_call_agent(self, operation, query, sources, request_id):
        return {"passages": [{"text": f"p{i}", "citationIndexes": []} for i in range(5)], "citations": []}

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    req = A2KRequest(operation="search", query="x", pagination=Pagination(limit=2))
    envelope = await engine._search_via_agent(req)
    assert len(envelope.passages) == 2
