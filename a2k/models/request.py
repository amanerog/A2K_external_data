"""Common A2K-KCP request fields (A2K-KCP-Consumption 4.md, sections 3-4)."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class RequestContext(BaseModel):
    userLocale: str | None = None
    jurisdiction: str | None = None
    dateContext: str | None = None
    purpose: str | None = None
    riskLevel: Literal["low", "medium", "high"] | None = None


class OnBehalfOf(BaseModel):
    """KCP section 3.1. Both providers here are tier S0 (public/commercial data),
    so `oboAssertionToken` is advisory context, not a validated gate -- see
    the KB Cards' `auth.oboAssertionRequired: false` and the README's tier
    rationale."""

    subject: str | None = None
    subjectIdP: str | None = None
    department: str | None = None
    roles: list[str] = Field(default_factory=list)
    entitlements: list[str] = Field(default_factory=list)
    clearanceLevel: str | None = None
    purpose: str | None = None
    oboAssertionToken: str | None = None


class AgentIdentity(BaseModel):
    agentId: str | None = None
    agentInstanceId: str | None = None
    serviceIdentity: str | None = None


class Requirements(BaseModel):
    citationsRequired: bool = True
    strictGrounding: bool = False
    maxStalenessHours: int | None = None
    answerLanguage: str | None = None
    allowedClassifications: list[str] = Field(default_factory=list)
    requireApprovedSources: bool = False
    reportConflicts: bool = True
    regulatedMode: bool = False


class RequestMetadata(BaseModel):
    requestId: str | None = None
    sessionId: str | None = None
    traceId: str | None = None


class Pagination(BaseModel):
    limit: int = 10
    cursor: str | None = None


class SearchFilters(BaseModel):
    language: str | None = None
    dateAsOf: str | None = None
    classificationAllowed: list[str] = Field(default_factory=list)
    lifecycleStatusAllowed: list[str] = Field(default_factory=list)


# Deliberately `str`, not Literal["cala", "sayari"] -- that hardcoded pair
# predates a2k/cards/__init__.py's DynamoDB-backed vendor catalogue and broke
# every new vendor at the request-validation layer, before engine.py even got
# a chance to look one up (confirmed live 2026-09-11, adding Linkup: every
# a2k.ask call with sources=["linkup"] failed pydantic validation here first).
# Real vendor-existence/active-status checking already happens downstream --
# gateway/engine.py's _search_via_agent/_ask_via_agent and direct_agent.py's
# handle() both filter `sources` against the live catalogue's active
# sourceIds -- so this only needs to be "a string", not an enum of vendors
# this module has no business knowing about.
Source = str


class A2KRequest(BaseModel):
    """Shared shape for search/ask; explain and getDocument have their own
    lightweight request models below because their KCP request bodies diverge."""

    a2kVersion: str = "0.6-baseline"
    operation: Literal["search", "ask"]
    query: str
    context: RequestContext | None = None
    onBehalfOf: OnBehalfOf | None = None
    agent: AgentIdentity | None = None
    requirements: Requirements = Field(default_factory=Requirements)
    requestMetadata: RequestMetadata | None = None
    filters: SearchFilters | None = None
    pagination: Pagination | None = None

    # Gateway-specific, non-normative: which backend KB(s) to fan out to.
    # Defaults to both when omitted.
    sources: list[Source] | None = None


class ExplainRequest(BaseModel):
    a2kVersion: str = "0.6-baseline"
    operation: Literal["explain"] = "explain"
    answerRef: str | None = None
    claimIds: list[str] = Field(default_factory=list)
    priorCitations: list[dict] = Field(default_factory=list)
    requirements: Requirements = Field(default_factory=Requirements)
    onBehalfOf: OnBehalfOf | None = None
    requestMetadata: RequestMetadata | None = None


class GetDocumentRequest(BaseModel):
    a2kVersion: str = "0.6-baseline"
    operation: Literal["getDocument"] = "getDocument"
    documentId: str
    onBehalfOf: OnBehalfOf | None = None
    requestMetadata: RequestMetadata | None = None
