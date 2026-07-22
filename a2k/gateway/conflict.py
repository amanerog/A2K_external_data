"""Cross-source conflict detection and reporting (A2K-KCP-Consumption 4.md,
section 12). Normative rule this module exists to satisfy: "when a material
conflict remains unresolved, agents MUST surface it rather than silently
choosing" (section 12.4). Nothing here picks a winner between Cala and
Sayari -- it only describes the disagreement so K2 (or the human it serves)
can decide.
"""

from __future__ import annotations

import itertools
from datetime import datetime, timezone

from ..models.envelope import (
    AwareConflict,
    AwareConflictSource,
    ConflictReport,
    ConflictReportEntry,
    ConflictReportProducer,
    ConflictReportResponseRef,
    ConflictReportSubject,
    EscalationTelemetry,
    Reconciliation,
)
from .synthesis import FactGroup, provider_label

FIELD_CONFLICT_TYPES = {
    "ultimate_beneficial_owner": "value-conflict",
    "entity_risk_rating": "interpretation-conflict",
}

HIGH_SEVERITY_FIELDS = {"ultimate_beneficial_owner", "ofac_sanctions_status"}


def build_aware_conflicts(
    grouped_claims: list[tuple[FactGroup, list]],
) -> tuple[list[AwareConflict], list[ConflictReportEntry]]:
    aware: list[AwareConflict] = []
    report_entries: list[ConflictReportEntry] = []
    counter = itertools.count(1)

    for group, claims in grouped_claims:
        if not group.disputed:
            continue
        conflict_id = f"conflict-{next(counter)}"
        conflict_type = FIELD_CONFLICT_TYPES.get(group.field, "value-conflict")
        values = list(group.facts_by_value.keys())
        primary_fact = group.facts_by_value[values[0]][0]
        other_fact = group.facts_by_value[values[1]][0]

        aware.append(
            AwareConflict(
                id=conflict_id,
                claimId=claims[0].id,
                nature=conflict_type,
                thisPosition=primary_fact.text,
                otherPosition=other_fact.text,
                otherSource=AwareConflictSource(
                    kbId=other_fact.kb_id, title=f"{provider_label(other_fact.kb_id)} entity profile"
                ),
                assessment="unresolved-disagreement",
                rationale=(
                    f"{provider_label(primary_fact.kb_id)} and {provider_label(other_fact.kb_id)} report "
                    f"different values for {group.field.replace('_', ' ')} of {group.entity_name}. "
                    "Neither is asserted as authoritative by this gateway -- surfaced per "
                    "A2K-KCP section 12.4 rather than resolved silently."
                ),
            )
        )
        report_entries.append(
            ConflictReportEntry(
                id=conflict_id,
                type=conflict_type,
                severity="high" if group.field in HIGH_SEVERITY_FIELDS else "medium",
                claims=[c.id for c in claims],
                summary=(
                    f"{group.entity_name}: {provider_label(primary_fact.kb_id)} and "
                    f"{provider_label(other_fact.kb_id)} disagree on {group.field.replace('_', ' ')}."
                ),
            )
        )

    return aware, report_entries


def build_conflict_report(
    *,
    query: str,
    gateway_agent_id: str,
    subject: str | None,
    kbs_queried: list[str],
    response_ref: str,
    report_entries: list[ConflictReportEntry],
    regulated_mode: bool,
) -> ConflictReport | None:
    if not report_entries:
        return None

    escalation = None
    if regulated_mode and any(entry.severity == "high" for entry in report_entries):
        escalation = EscalationTelemetry(
            required=True,
            route="grc-case-management",
            caseType="vendor-data-conflict",
            ownerTeams=["Compliance", "Data Governance"],
            severity="high",
            sla="5-business-days",
            createdTicketRef=None,
        )

    return ConflictReport(
        query=query,
        producedBy=ConflictReportProducer(role="knowledge-gateway", id=gateway_agent_id),
        producedAt=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        onBehalfOf=ConflictReportSubject(subject=subject) if subject else None,
        kbsQueried=kbs_queried,
        # Simplification: this gateway fans out at fact granularity rather than
        # materializing one full KCP envelope per backend KB, so all conflict
        # entries reference the single combined gateway response rather than
        # distinct per-KB response envelopes (see README, "Simplificaciones").
        responses=[ConflictReportResponseRef(sourceKbId=kb_id, responseRef=response_ref) for kb_id in kbs_queried],
        conflicts=report_entries,
        reconciliation=Reconciliation(
            status="unresolved-surfaced",
            recommendedAction=(
                "Do not treat either source as authoritative for the disputed field(s). "
                "Corroborate against a primary filing or escalate before using this in a decision."
            ),
            basis=["scope-match", "corroboration", "freshness"],
        ),
        escalationTelemetry=escalation,
        audit={"logged": True},
    )
