"""The Pydantic models validate the shapes shown in A2K-KCP-Consumption 4.md."""

from a2k.models.envelope import (
    AccessDecision,
    Citation,
    CitedResponseEnvelope,
    Claim,
    Grounding,
    TextQuoteSelector,
)
from a2k.models.kbcard import KBCard
from a2k.cards import load_card


def test_ask_envelope_matches_spec_shape():
    envelope = CitedResponseEnvelope(
        ok=True,
        operation="ask",
        sourceKbId="urn:a2k:enterprise:example-kb",
        answer="Employees may expense business-class flights only with VP approval.",
        claims=[
            Claim(
                id="claim-1",
                text="Business-class air travel requires VP approval.",
                type="policy",
                status="SUPPORTED",
                citationIds=["citation-1"],
            )
        ],
        citations=[
            Citation(
                id="citation-1",
                claimIds=["claim-1"],
                documentId="doc:expense-policy:air-travel:v3",
                title="Employee Expense Policy",
                selector=TextQuoteSelector(exact="Business-class air travel requires VP approval."),
                retrievedAt="2026-07-07T09:12:00Z",
            )
        ],
        grounding=Grounding(groundedRatio=1.0, strictGroundingSatisfied=True),
        accessDecision=AccessDecision(decision="allowed"),
    )
    dumped = envelope.model_dump(mode="json")
    assert dumped["a2kVersion"] == "0.6-baseline"
    assert dumped["claims"][0]["status"] == "SUPPORTED"
    assert dumped["citations"][0]["selector"]["type"] == "TextQuoteSelector"


def test_search_envelope_has_null_answer():
    envelope = CitedResponseEnvelope(ok=True, operation="search", sourceKbId="urn:a2k:vendor:cala")
    assert envelope.answer is None
    assert envelope.passages == []


def test_error_envelope_shape():
    from a2k.models.envelope import ErrorObject

    envelope = CitedResponseEnvelope(
        ok=False,
        operation="ask",
        sourceKbId="urn:a2k:gateway:k2-external-intel",
        error=ErrorObject(code="NOT_FOUND", message="No matching entity."),
    )
    assert envelope.ok is False
    assert envelope.error.code == "NOT_FOUND"


def test_all_three_kb_cards_validate():
    for name in ("gateway", "cala", "sayari"):
        card = load_card(name)
        assert isinstance(card, KBCard)
        assert card.conformance.level == 4
        assert card.enterprise.access.derived_security_tier == "S0"
