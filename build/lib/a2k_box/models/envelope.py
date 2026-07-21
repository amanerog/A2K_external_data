"""Cited-response envelope and related models (A2K-KCP-Consumption 4.md, sections 5-17).

Field names intentionally mirror the wire-format JSON keys (camelCase) from the
spec verbatim, so these models can be dumped with `model_dump(mode="json")`
and match the .md examples field-for-field.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# --- Span selectors (KCP section 7.2, W3C Web Annotation Data Model) ---------


class TextQuoteSelector(BaseModel):
    type: Literal["TextQuoteSelector"] = "TextQuoteSelector"
    exact: str
    prefix: str | None = None
    suffix: str | None = None


class TextPositionSelector(BaseModel):
    type: Literal["TextPositionSelector"] = "TextPositionSelector"
    start: int
    end: int


class FragmentSelector(BaseModel):
    type: Literal["FragmentSelector"] = "FragmentSelector"
    value: str
    conformsTo: str | None = None


Selector = TextQuoteSelector | TextPositionSelector | FragmentSelector


# --- Data lineage (KCP section 7.4, Level 4) ---------------------------------


class LineageStep(BaseModel):
    step: str
    processedAt: str | None = None
    ingestedAt: str | None = None
    sourcePipeline: str | None = None
    chunkerVersion: str | None = None
    embeddingModel: str | None = None
    embeddingModelVersion: str | None = None

    model_config = ConfigDict(extra="allow")


# --- Citations (KCP section 7) -----------------------------------------------


class Citation(BaseModel):
    id: str
    claimIds: list[str] = Field(default_factory=list)
    documentId: str | None = None
    title: str | None = None
    selector: Selector | None = None
    sourceHash: str | None = None
    sourceUrl: str | None = None
    retrievedAt: str
    sourceLastUpdated: str | None = None
    classification: str | None = None
    dataLineage: list[LineageStep] = Field(default_factory=list)


# --- Claims (KCP section 6) --------------------------------------------------

ClaimStatus = Literal[
    "SUPPORTED",
    "REFUTED",
    "DISPUTED",
    "INSUFFICIENT_EVIDENCE",
    "OUT_OF_SCOPE",
    "ACCESS_DENIED",
    "UNAUDITED_MATERIAL",
    "AMBIGUOUS_QUERY",
    "HUMAN_REVIEW_REQUIRED",
]


class Claim(BaseModel):
    id: str
    text: str
    type: str | None = None
    status: ClaimStatus
    citationIds: list[str] = Field(default_factory=list)
    conflictsWith: list[str] = Field(default_factory=list)


# --- Passages (search, KCP section 4.1) --------------------------------------


class Passage(BaseModel):
    id: str
    text: str
    documentId: str | None = None
    score: float | None = None
    citationIds: list[str] = Field(default_factory=list)


# --- Grounding (KCP section 8) -----------------------------------------------

ConfidenceMethod = Literal[
    "calibrated", "nli-entailment", "retrieval-similarity", "heuristic", "llm-self-report", "unknown"
]


class Grounding(BaseModel):
    groundedRatio: float
    ungroundedSpans: list[TextPositionSelector] = Field(default_factory=list)
    confidence: float | None = None
    confidenceMethod: ConfidenceMethod | None = None
    strictGroundingSatisfied: bool


# --- Freshness (KCP section 9) -----------------------------------------------


class Freshness(BaseModel):
    sourceLastUpdated: str | None = None
    reviewedAt: str | None = None
    nextReviewDue: str | None = None
    retrievedAt: str
    stale: bool
    validAsOf: str
    validUntil: str | None = None


# --- Access decision (KCP section 10) ----------------------------------------

AccessDecisionValue = Literal[
    "allowed", "denied", "redacted", "partial", "restricted-by-compliance", "human-review-required", "unknown"
]


class AccessDecision(BaseModel):
    decision: AccessDecisionValue
    reason: str | None = None
    appliedScopes: list[str] = Field(default_factory=list)
    dataClassification: str | None = None
    policyEngine: str | None = None
    decisionId: str | None = None
    oboValidated: bool | None = None


# --- Audit (KCP section 11) --------------------------------------------------


class Audit(BaseModel):
    requestId: str | None = None
    sessionId: str | None = None
    traceId: str | None = None
    agentId: str | None = None
    agentInstanceId: str | None = None
    userId: str | None = None
    sourceKbId: str | None = None
    sourceKbVersion: str | None = None
    kbCardVersion: str | None = None
    operation: str | None = None
    policyDecision: str | None = None
    decisionReason: str | None = None
    citationIds: list[str] = Field(default_factory=list)
    logged: bool
    loggedAt: str | None = None
    logTarget: str | None = None
    immutableLogRequired: bool | None = None
    logRef: str | None = None


# --- Conflicts (KCP section 12) ----------------------------------------------

ConflictType = Literal[
    "value-conflict",
    "scope-conflict",
    "temporal-conflict",
    "interpretation-conflict",
    "methodology-conflict",
    "authority-collision",
    "freshness-conflict",
    "access-conditioned-conflict",
    "unknown",
]


class AwareConflictSource(BaseModel):
    kbId: str
    title: str | None = None


class AwareConflict(BaseModel):
    """Self-reported conflict embedded directly in an envelope's `conflicts[]` (KCP 12.1)."""

    id: str
    claimId: str
    nature: ConflictType
    thisPosition: str
    otherPosition: str
    otherSource: AwareConflictSource
    assessment: str
    rationale: str


class ConflictReportEntry(BaseModel):
    """A single conflict inside a gateway-level ConflictReport (KCP 12.2)."""

    id: str
    type: ConflictType
    severity: Literal["low", "medium", "high"]
    claims: list[str] = Field(default_factory=list)
    summary: str


class EscalationTelemetry(BaseModel):
    required: bool
    route: str | None = None
    caseType: str | None = None
    ownerTeams: list[str] = Field(default_factory=list)
    severity: str | None = None
    sla: str | None = None
    createdTicketRef: str | None = None


class Reconciliation(BaseModel):
    status: str
    recommendedAction: str | None = None
    basis: list[str] = Field(default_factory=list)


class ConflictReportResponseRef(BaseModel):
    sourceKbId: str
    responseRef: str


class ConflictReportProducer(BaseModel):
    role: str
    id: str


class ConflictReportSubject(BaseModel):
    subject: str


class ConflictReport(BaseModel):
    """Gateway-level cross-KB conflict report artifact (KCP section 12.2).

    Distinct from the `conflicts` field embedded in a cited-response envelope.
    """

    a2kVersion: str = "0.6-baseline"
    kind: Literal["conflictReport"] = "conflictReport"
    query: str
    producedBy: ConflictReportProducer
    producedAt: str
    onBehalfOf: ConflictReportSubject | None = None
    kbsQueried: list[str]
    responses: list[ConflictReportResponseRef]
    conflicts: list[ConflictReportEntry]
    reconciliation: Reconciliation
    escalationTelemetry: EscalationTelemetry | None = None
    audit: dict[str, Any] | None = None


# --- Referrals (KCP section 13) ----------------------------------------------


class Referral(BaseModel):
    kbId: str
    reason: str
    authorityLevel: str | None = None
    cardUrl: str | None = None


# --- Usage ---------------------------------------------------------------------


class Usage(BaseModel):
    latencyMs: int | None = None
    retrievalCount: int | None = None


# --- Pagination (KCP section 5.1) --------------------------------------------


class PageInfo(BaseModel):
    nextCursor: str | None = None
    hasMore: bool
    pageLimit: int


# --- Response signature (KCP section 14.3, Level 4) --------------------------


class ResponseSignature(BaseModel):
    alg: str
    kid: str
    canonicalization: str
    signedFields: list[str]
    jws: str


# --- Error object (KCP section 17) -------------------------------------------


class ErrorObject(BaseModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


# --- The cited-response envelope (KCP section 5) ------------------------------

Operation = Literal["search", "ask", "explain", "getDocument"]


class CitedResponseEnvelope(BaseModel):
    a2kVersion: str = "0.6-baseline"
    ok: bool
    operation: Operation
    sourceKbId: str
    answer: str | None = None
    passages: list[Passage] = Field(default_factory=list)
    claims: list[Claim] = Field(default_factory=list)
    citations: list[Citation] = Field(default_factory=list)
    grounding: Grounding | None = None
    freshness: Freshness | None = None
    accessDecision: AccessDecision | None = None
    audit: Audit | None = None
    conflicts: list[AwareConflict] = Field(default_factory=list)
    referrals: list[Referral] = Field(default_factory=list)
    usage: Usage | None = None
    pageInfo: PageInfo | None = None
    responseSignature: ResponseSignature | None = None
    error: ErrorObject | None = None

    # Gateway-level extension (informative, non-normative): when this envelope
    # is the result of fanning out to multiple KBs, the full conflict report
    # (if any conflicts were found) travels alongside the envelope so callers
    # get both the terse per-claim conflicts[] and the full artifact.
    conflictReport: ConflictReport | None = None


# --- getDocument response (KCP section 4.4) -----------------------------------


class Document(BaseModel):
    documentId: str
    title: str | None = None
    sourceUrl: str | None = None
    mimeType: str
    content: str | None = None
    hash: str | None = None
    lastUpdated: str | None = None
    classification: str | None = None


class GetDocumentResponse(BaseModel):
    a2kVersion: str = "0.6-baseline"
    ok: bool
    operation: Literal["getDocument"] = "getDocument"
    sourceKbId: str
    document: Document | None = None
    accessDecision: AccessDecision | None = None
    error: ErrorObject | None = None
