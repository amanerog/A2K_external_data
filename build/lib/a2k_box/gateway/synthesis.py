"""Facts -> passages/claims/citations/answer, with exact, checkable grounding.

Design choice (see plan): the gateway never calls an LLM to write `answer`.
It only concatenates fact text that is already, verbatim, the `exact` quote
of a citation. That makes `groundedRatio` an exact character count rather
than an estimate, and makes `strictGrounding` satisfiable deterministically
(section 8.1 of A2K-KCP-Consumption).
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field

from ..adapters.base import Fact
from ..models.envelope import Citation, Claim, LineageStep, Passage, TextPositionSelector, TextQuoteSelector

FIELD_CLAIM_TYPES = {
    "incorporation_date": "factual",
    "annual_revenue_usd": "quantitative",
    "ofac_sanctions_status": "factual",
    "ultimate_beneficial_owner": "factual",
    "entity_risk_rating": "interpretive",
    "corporate_relationships": "factual",
}


def provider_label(kb_id: str) -> str:
    if "cala" in kb_id:
        return "Cala"
    if "sayari" in kb_id:
        return "Sayari"
    return kb_id


def humanize_field(field_name: str) -> str:
    return field_name.replace("_", " ")


@dataclass
class FactGroup:
    entity_key: str
    entity_name: str
    field: str
    facts_by_value: dict[str, list[Fact]] = field(default_factory=dict)

    @property
    def disputed(self) -> bool:
        return len(self.facts_by_value) > 1


def group_facts(facts: list[Fact]) -> list[FactGroup]:
    """Group facts by (entity, field) -- the join key for conflict detection.

    Preserves first-seen order so the synthesized answer reads in a stable,
    predictable sequence across requests.
    """
    groups: dict[tuple[str, str], FactGroup] = {}
    order: list[tuple[str, str]] = []
    for fact in facts:
        key = (fact.entity_key, fact.field)
        if key not in groups:
            groups[key] = FactGroup(entity_key=fact.entity_key, entity_name=fact.entity_name, field=fact.field)
            order.append(key)
        groups[key].facts_by_value.setdefault(fact.value, []).append(fact)
    return [groups[k] for k in order]


def _citation_from_fact(fact: Fact, citation_id: str, claim_id: str) -> Citation:
    return Citation(
        id=citation_id,
        claimIds=[claim_id],
        documentId=fact.document_id,
        title=fact.title,
        selector=TextQuoteSelector(exact=fact.text),
        sourceHash=fact.source_hash,
        sourceUrl=fact.source_url,
        retrievedAt=fact.retrieved_at,
        sourceLastUpdated=fact.source_last_updated,
        classification=fact.classification,
        dataLineage=[
            LineageStep(
                step="provider-fetch",
                sourcePipeline=f"{provider_label(fact.kb_id).lower()}-adapter-v1",
                ingestedAt=fact.retrieved_at,
            )
        ],
    )


def build_claims_and_citations(
    groups: list[FactGroup],
) -> tuple[list[tuple[FactGroup, list[Claim]]], list[Citation]]:
    citations: list[Citation] = []
    grouped_claims: list[tuple[FactGroup, list[Claim]]] = []
    claim_n = itertools.count(1)
    citation_n = itertools.count(1)

    for group in groups:
        group_claims: list[Claim] = []
        for value, facts in group.facts_by_value.items():
            claim_id = f"claim-{next(claim_n)}"
            citation_ids = []
            for fact in facts:
                citation_id = f"citation-{next(citation_n)}"
                citations.append(_citation_from_fact(fact, citation_id, claim_id))
                citation_ids.append(citation_id)
            status = "DISPUTED" if group.disputed else "SUPPORTED"
            group_claims.append(
                Claim(
                    id=claim_id,
                    text=facts[0].text,
                    type=FIELD_CLAIM_TYPES.get(group.field, "factual"),
                    status=status,
                    citationIds=citation_ids,
                    conflictsWith=[],
                )
            )
        if group.disputed:
            all_ids = [c.id for c in group_claims]
            for claim in group_claims:
                claim.conflictsWith = [cid for cid in all_ids if cid != claim.id]
        grouped_claims.append((group, group_claims))

    return grouped_claims, citations


def synthesize_answer(
    grouped_claims: list[tuple[FactGroup, list[Claim]]], *, strict: bool
) -> tuple[str, list[TextPositionSelector]]:
    """Build `answer` as a sequence of segments, each tagged grounded/ungrounded.

    Grounded segments are always a claim's `text` verbatim (identical to its
    citation's `selector.exact`). Under `strict=True` the disputed-group
    connector sentence is dropped entirely so the answer is 100% extractive;
    the disagreement is still surfaced structurally via `conflicts`/claim
    `status: DISPUTED`, never silently resolved (A2K-KCP section 12.4).
    """
    segments: list[tuple[str, bool]] = []
    for group_index, (group, claims) in enumerate(grouped_claims):
        if group_index > 0:
            segments.append(("\n\n", True))
        if not group.disputed:
            segments.append((claims[0].text, True))
            continue
        if not strict:
            segments.append(
                (
                    f"On {humanize_field(group.field)} for {group.entity_name}, sources disagree: ",
                    False,
                )
            )
        for claim_index, claim in enumerate(claims):
            if claim_index > 0:
                segments.append((" ", True))
            segments.append((claim.text, True))

    return assemble_segments(segments)


def assemble_segments(segments: list[tuple[str, bool]]) -> tuple[str, list[TextPositionSelector]]:
    """Join (text, grounded) segments into text + exact ungrounded-span offsets."""
    answer = "".join(text for text, _ in segments)
    ungrounded: list[TextPositionSelector] = []
    cursor = 0
    for text, grounded in segments:
        start, end = cursor, cursor + len(text)
        if not grounded:
            ungrounded.append(TextPositionSelector(start=start, end=end))
        cursor = end
    return answer, ungrounded


def compute_grounded_ratio(answer: str, ungrounded_spans: list[TextPositionSelector]) -> float:
    if not answer:
        return 0.0
    ungrounded_chars = sum(min(span.end, len(answer)) - span.start for span in ungrounded_spans)
    ratio = 1 - (ungrounded_chars / len(answer))
    return max(0.0, min(1.0, round(ratio, 4)))


def build_passages_and_citations(facts: list[Fact]) -> tuple[list[Passage], list[Citation]]:
    """search's output shape: one passage + one citation per fact, no claim grouping."""
    passages: list[Passage] = []
    citations: list[Citation] = []
    passage_n = itertools.count(1)
    citation_n = itertools.count(1)
    for fact in facts:
        passage_id = f"passage-{next(passage_n)}"
        citation_id = f"citation-{next(citation_n)}"
        citations.append(_citation_from_fact(fact, citation_id, claim_id=""))
        citations[-1].claimIds = []
        passages.append(
            Passage(
                id=passage_id,
                text=fact.text,
                documentId=fact.document_id,
                score=fact.score,
                citationIds=[citation_id],
            )
        )
    return passages, citations
