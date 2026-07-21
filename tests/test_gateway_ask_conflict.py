"""The normative rule this suite exists to check (A2K-KCP section 12.4):
"when a material conflict remains unresolved, agents MUST surface it rather
than silently choosing." Meridian Textiles' fixtures deliberately disagree
between Cala and Sayari on ultimate_beneficial_owner; Acme Robotics'
fixtures deliberately agree on everything they share.
"""

import pytest

from a2k_box.gateway.engine import GatewayEngine
from a2k_box.models.request import A2KRequest, Requirements


@pytest.fixture
def engine():
    return GatewayEngine()


async def test_conflicting_sources_are_surfaced_not_resolved(engine):
    req = A2KRequest(operation="ask", query="Meridian Textiles ownership")
    envelope = await engine.ask(req)

    assert envelope.ok is True
    assert len(envelope.conflicts) == 1
    conflict = envelope.conflicts[0]
    assert conflict.nature == "value-conflict"
    assert "62%" in conflict.thisPosition
    assert "48%" in conflict.otherPosition

    disputed = [c for c in envelope.claims if c.status == "DISPUTED"]
    assert len(disputed) == 2
    assert disputed[0].conflictsWith == [disputed[1].id]

    assert envelope.conflictReport is not None
    assert envelope.conflictReport.reconciliation.status == "unresolved-surfaced"
    # never silently picks a winner
    assert "Do not treat either source as authoritative" in envelope.conflictReport.reconciliation.recommendedAction


async def test_agreeing_sources_produce_no_conflict(engine):
    req = A2KRequest(operation="ask", query="Acme Robotics")
    envelope = await engine.ask(req)

    assert envelope.ok is True
    assert envelope.conflicts == []
    assert envelope.conflictReport is None
    assert all(c.status == "SUPPORTED" for c in envelope.claims)


async def test_strict_grounding_is_fully_extractive(engine):
    req = A2KRequest(
        operation="ask",
        query="Meridian Textiles ownership",
        requirements=Requirements(strictGrounding=True),
    )
    envelope = await engine.ask(req)

    assert envelope.ok is True
    assert envelope.grounding.groundedRatio == 1.0
    assert envelope.grounding.strictGroundingSatisfied is True
    assert envelope.grounding.ungroundedSpans == []
    # the disagreement is still surfaced structurally even though the prose
    # dropped the "sources disagree" connector
    assert len(envelope.conflicts) == 1


async def test_no_matching_entity_returns_insufficient_evidence(engine):
    req = A2KRequest(operation="ask", query="Totally Unknown Company XYZ")
    envelope = await engine.ask(req)

    assert envelope.ok is True
    assert envelope.answer is None
    assert envelope.claims[0].status == "INSUFFICIENT_EVIDENCE"


async def test_single_source_restricts_fanout(engine):
    req = A2KRequest(operation="ask", query="Meridian Textiles", sources=["cala"])
    envelope = await engine.ask(req)

    assert envelope.sourceKbId == "urn:a2k:vendor:cala"
    assert envelope.conflicts == []  # only one source queried, nothing to disagree with
