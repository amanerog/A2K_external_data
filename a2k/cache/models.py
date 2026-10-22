"""Data carried between admission steps -- the in-process equivalent of the
Step Functions state in the proposal's Annex A4. Each step reads what earlier
steps produced and adds its own fields; only `admission.activate` writes the
result back to Aurora.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal


@dataclass
class Candidate:
    """A vendor answer just served to the user. Built synchronously in the
    request path (cheap: no I/O), everything else happens in the background."""

    entry_id: str
    question: str
    answer: str
    vendor: str
    # The agent's own toolCalls shape: vendor, toolName, input, output,
    # status ("success"/"error"), errorType. AskContent (the structured-output
    # tool the agent uses to hand back its answer) is not vendor data and is
    # dropped before anything reads this.
    tool_calls: list[dict[str, Any]]
    citations: list[dict[str, Any]]
    created_at: datetime
    # Revision of the vendor's KB Card when the answer was produced: the entry
    # stops being served when it changes (A2K-KCP 0.1 section 9.4).
    card_revision: str | None = None
    # The agent's self-reported groundedRatio, served back with a cache hit.
    grounded_ratio: float | None = None
    # The agent's claims, each written verbatim from the answer, with the URLs
    # of the citations that support it: [{"text", "sources": [url, ...]}].
    claims: list[dict[str, Any]] = field(default_factory=list)
    # The agent's own reading: "complete" | "partial" | "none" (None if not given).
    coverage: str | None = None
    # What the answer took (savings.answer_cost): vendor tool calls per vendor
    # and the agent's tokens -- what serving this entry later saves.
    answer_cost: dict[str, Any] | None = None


@dataclass
class RequestedElement:
    id: str  # "E1", "E2"... -- the judge reports coverage by this id
    element: str
    expected_type: str = "text"
    constraints: str = ""


@dataclass
class Classification:
    """What the admission classifier (prompts/clasificador_admision_v1.md)
    returns. `raw` is its JSON as returned: the judge receives it as-is."""

    domain_primary: str
    domain_secondary: str | None
    requested_elements: list[RequestedElement]
    # [{"name", "kind", "identifiers"}]
    entities: list[dict[str, Any]]
    # {"kind": "relative" | "absolute" | "none", "text", "from", "to", "entirely_past"}
    time_anchor: dict[str, Any]
    # A question that depends on earlier turns ("and its LEI?") cannot be
    # reused for other users: NOT FIT before the judge is paid.
    self_contained: bool = True
    question_type: str | None = None
    depth: str | None = None
    language: str | None = None  # informative only: the stored language comes from retrieval's detector
    notes: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def domains(self) -> list[str]:
        return [d for d in (self.domain_primary, self.domain_secondary) if d]


CheckResult = Literal["pass", "fail", "not_applicable"]


@dataclass
class ElementFinding:
    id: str
    element: str
    status: Literal["covered", "partial", "missing"]
    # Verbatim passage of the raw tool output (our addition to the judge
    # prompt) -- checked in code, so grounding does not rest on the model alone.
    evidence_quote: str | None
    # Is the data of the element's expected_type? None when missing. Feeds C2.
    data_type_ok: bool | None = None
    evidence: str = ""  # where the answer gives it, in the judge's words
    # Set by the decision rule, not the model: does evidence_quote really
    # appear in the raw tool output?
    quote_verified: bool | None = None


@dataclass
class JudgeFindings:
    elements: list[ElementFinding]
    core: dict[str, dict[str, str]]  # {"C1": {"result": ..., "reason": ...}, ...}
    modules: dict[str, dict[str, str]]  # {"REG-1": {"result": ..., "reason": ...}, ...}
    data_as_of: date | None
    # "current": the vendor presents the data as it stands today (a registry
    # record, a rule in force, a current board) -- expiry counts from
    # admission. "snapshot": dated (news, report, a figure for a period) --
    # expiry counts from data_as_of.
    data_kind: str = "snapshot"
    unsupported_claims: list[str] = field(default_factory=list)
    justification: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


Outcome = Literal["ACTIVE", "NOT_FIT", "FILTERED", "DOMAIN_OFF", "STALE", "SUPERSEDED", "ERROR"]


@dataclass
class Normalized:
    original_lang: str
    question_en: str
    answer_en: str
    # The claims translated with the answer, in the same order (empty when the
    # translation did not return one per claim).
    claims_en: list[str] = field(default_factory=list)


@dataclass
class AdmissionResult:
    entry_id: str
    outcome: Outcome
    reason: str
    classification: Classification | None = None
    findings: JudgeFindings | None = None
    # The second, confirming judgement of a FIT verdict (confirm_fit).
    confirmation: JudgeFindings | None = None
    normalized: Normalized | None = None
    expires_at: datetime | None = None
    verified_sources: list[dict[str, Any]] = field(default_factory=list)
    timings_ms: dict[str, int] = field(default_factory=dict)
    # What the judge read in basic mode: {"mode": full|cited|capped, "chars",
    # "full_chars", ...} (admission._judge_evidence).
    evidence: dict[str, Any] | None = None
