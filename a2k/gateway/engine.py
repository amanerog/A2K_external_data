"""GatewayEngine: the one place that knows how to answer search/ask/explain/
getDocument. `api/rest.py` and `mcp_server/server.py` are thin transport
adapters over this class -- see plan decision "un unico motor, dos
transportes".
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

import httpx

from ..adapters.base import Fact, ProviderAdapter
from ..adapters.cala_mcp import CalaMcpAdapter
from ..adapters.sayari_mcp import SayariMcpAdapter
from ..cards import vendor_catalogue
from ..config import config
from ..errors import A2KError, ErrorCode
from ..models.envelope import (
    AccessDecision,
    AwareConflict,
    AwareConflictSource,
    Citation,
    CitedResponseEnvelope,
    Claim,
    Document,
    ErrorObject,
    Freshness,
    GetDocumentResponse,
    Grounding,
    Passage,
    ToolCallRecord,
    Usage,
)
from ..models.request import A2KRequest, ExplainRequest, GetDocumentRequest
from . import audit as gw_audit
from . import conflict, synthesis, tracing
from ..cache import admission, retrieval
from ..cache.models import Candidate
from ..cache.store import AuroraStore, S3EvidenceStore

GATEWAY_KB_ID = "urn:a2k:gateway:k2-external-intel"
_CACHE_MAX_SIZE = 500


def _admission_settings() -> admission.AdmissionSettings:
    return admission.AdmissionSettings(
        light_model_id=config.admission_light_model_id,
        judge_model_id=config.admission_judge_model_id,
        embedding_model_id=config.cache_embedding_model_id,
        min_answer_chars=config.admission_min_answer_chars,
        max_evidence_chars=config.admission_max_evidence_chars,
        require_verified_quotes=config.admission_require_verified_quotes,
        fallback_messages=(config.vendor_fallback_message,),
        allowed_vendors=config.admission_allowed_vendors,
        workers=config.admission_workers,
        disabled_domains=config.cache_disabled_domains,
        confirm_fit=config.admission_confirm_fit,
        mode=config.admission_mode,
        grounding_domains=config.admission_grounding_domains,
    )


def _retrieval_settings() -> retrieval.RetrievalSettings:
    return retrieval.RetrievalSettings(
        light_model_id=config.admission_light_model_id,
        checker_model_id=config.cache_checker_model_id,
        embedding_model_id=config.cache_embedding_model_id,
        disabled_domains=config.cache_disabled_domains,
        reformulation_window_seconds=config.cache_reformulation_window_seconds,
    )


_cache_clients_value = None


def _cache_clients():
    """(retrieval bedrock-runtime, retrieval store, admission store, S3
    evidence store, admission bedrock-runtime), built once on first use --
    inside an admission worker or a retrieval thread, never before the cache
    is actually turned on. boto3 clients are thread-safe and shared. Two
    Aurora stores over the same client: only the admission one waits for a
    paused cluster to resume (see AuroraStore).

    Two Bedrock clients, because the two sides wait very differently. A
    judgement takes 1-5 min with no bytes until the end (non-streaming
    Converse): with boto3's default 60 s read timeout and 4 retries, the
    runtime re-sent every judgement up to 4 times, each one paid and each one
    adding load -- 2 of 9 admissions ended in ReadTimeoutError and 2 more in
    ServiceUnavailableException (shadow run, 2026-10-05). Retrieval calls
    (Haiku, Titan, the Checker) take seconds and live inside the 10 s budget,
    so a short timeout and a single retry are right there."""
    global _cache_clients_value
    if _cache_clients_value is None:
        import boto3
        from botocore.config import Config as BotoConfig

        rds_data = boto3.client("rds-data")
        aurora = dict(
            rds_data_client=rds_data,
            cluster_arn=config.aurora_cluster_arn,
            secret_arn=config.aurora_secret_arn,
            database=config.aurora_database,
        )
        _cache_clients_value = (
            boto3.client("bedrock-runtime", config=BotoConfig(read_timeout=30, retries={"max_attempts": 2, "mode": "standard"})),
            AuroraStore(**aurora),
            AuroraStore(**aurora, resume_retries=9),
            S3EvidenceStore(s3_client=boto3.client("s3"), bucket=config.cache_evidence_bucket),
            boto3.client("bedrock-runtime", config=BotoConfig(read_timeout=600, retries={"max_attempts": 4, "mode": "adaptive"})),
        )
    return _cache_clients_value


def _admission_deps():
    _, _, admission_store, evidence_store, bedrock_client = _cache_clients()
    return bedrock_client, admission_store, evidence_store


# ConflictType's allowed values (models/envelope.py) -- the agent's own
# self-reported `nature` string (direct_agent.py's ConflictOut) isn't
# guaranteed to land on one of these exactly, so it's validated against this
# set rather than trusted, falling back to "unknown" (still a valid,
# documented ConflictType) rather than raising a Pydantic validation error
# over a field an LLM free-texted.
_VALID_CONFLICT_TYPES = {
    "value-conflict",
    "scope-conflict",
    "temporal-conflict",
    "interpretation-conflict",
    "methodology-conflict",
    "authority-collision",
    "freshness-conflict",
    "access-conditioned-conflict",
    "unknown",
}


class GatewayEngine:
    def __init__(self) -> None:
        # Both providers default to their own hosted MCP servers now
        # (adapters/cala_mcp.py, adapters/sayari_mcp.py) rather than REST --
        # the REST adapters (adapters/cala.py, adapters/sayari.py) still
        # exist and work, swap here if you need that transport instead.
        self.adapters: dict[str, ProviderAdapter] = {"cala": CalaMcpAdapter(), "sayari": SayariMcpAdapter()}
        self.gateway_kb_id = GATEWAY_KB_ID
        # Cognito bearer token for calling the agent's own Runtime directly
        # (mcp_to_agent_to_mcp branch -- ask()/search() below) -- separate
        # pool/credential from anything the adapters above use. See
        # config.py's agent_pool_* fields and _get_agent_token() below.
        self._agent_token: str | None = None
        self._agent_token_expires_at: float = 0.0
        self._response_cache: dict[str, CitedResponseEnvelope] = {}

    # -- operations -------------------------------------------------------

    async def search(self, req: A2KRequest) -> CitedResponseEnvelope:
        """mcp_to_agent_to_mcp branch: mock mode keeps the original
        deterministic _gather_facts()/adapters path (_search_deterministic)
        so the existing test suite -- which only ever runs in mock mode --
        stays meaningful and green; that path can't be exercised by the new
        agent-mediated one at all (it needs a real Bedrock+deployed-agent
        round trip, not something a unit test can reproduce). Live mode uses
        the new path (_search_via_agent) -- see that method's own docstring
        (via _call_agent) for the trade-off this branch makes."""
        if config.is_mock:
            return await self._search_deterministic(req)
        return await self._search_via_agent(req)

    async def _search_deterministic(self, req: A2KRequest) -> CitedResponseEnvelope:
        request_id = self._request_id(req)
        sources = req.sources or list(self.adapters)
        source_kb_id = self._source_kb_id(sources)
        limit = req.pagination.limit if req.pagination else 10
        t0 = time.monotonic()
        tracing.trace("engine.search.request", requestId=request_id, query=req.query, sources=sources, limit=limit)

        try:
            facts_by_source = await self._gather_facts(req.query, sources, limit=limit)
        except A2KError as err:
            return self._error_envelope("search", source_kb_id, err, request_id)

        all_facts = [f for facts in facts_by_source.values() for f in facts][:limit]
        passages, citations = synthesis.build_passages_and_citations(all_facts)
        freshness = self._freshness(all_facts, req.requirements.maxStalenessHours)

        audit = gw_audit.write_audit(
            request_id=request_id,
            session_id=req.requestMetadata.sessionId if req.requestMetadata else None,
            agent_id=req.agent.agentId if req.agent else None,
            user_id=req.onBehalfOf.subject if req.onBehalfOf else None,
            source_kb_id=source_kb_id,
            operation="search",
            policy_decision="allowed",
            decision_reason=None,
            citation_ids=[c.id for c in citations],
        )

        envelope = CitedResponseEnvelope(
            ok=True,
            operation="search",
            sourceKbId=source_kb_id,
            answer=None,
            passages=passages,
            citations=citations,
            freshness=freshness,
            accessDecision=self._access_decision(),
            audit=audit,
            usage=Usage(latencyMs=self._elapsed_ms(t0), retrievalCount=len(all_facts)),
            pageInfo={"nextCursor": None, "hasMore": False, "pageLimit": limit},
        )
        self._cache(request_id, envelope)
        tracing.trace(
            "engine.search.response",
            requestId=request_id,
            ok=True,
            passageCount=len(passages),
            citationCount=len(citations),
        )
        return envelope

    async def _search_via_agent(self, req: A2KRequest) -> CitedResponseEnvelope:
        """Live-mode path (mcp_to_agent_to_mcp branch): delegates to the agent
        (direct vendor-MCP discovery) instead of the deterministic
        _gather_facts()/adapters path above -- see _call_agent()'s docstring
        for the full trade-off.

        Not cached: the cache (a2k/cache/) stores questions with a written
        answer, and search returns passages, not an answer."""
        request_id = self._request_id(req)
        sources = req.sources or list(self.adapters)
        source_kb_id = self._source_kb_id(sources)
        limit = req.pagination.limit if req.pagination else 10
        t0 = time.monotonic()
        tracing.trace("engine.search.request", requestId=request_id, query=req.query, sources=sources, limit=limit)

        try:
            content = await self._call_agent("search", req.query, req.sources, request_id)
        except A2KError as err:
            return self._error_envelope("search", source_kb_id, err, request_id)
        retrieved_at = None
        decision_reason = None

        citations = [
            self._citation_from_agent(i, c, retrieved_at=retrieved_at)
            for i, c in enumerate(content.get("citations") or [])
        ]
        passages = [
            Passage(
                id=f"passage-{i + 1}",
                text=p.get("text", ""),
                citationIds=self._citation_ids_for(citations, p.get("citationIndexes") or []),
            )
            for i, p in enumerate(content.get("passages") or [])
        ][:limit]

        audit = gw_audit.write_audit(
            request_id=request_id,
            session_id=req.requestMetadata.sessionId if req.requestMetadata else None,
            agent_id=req.agent.agentId if req.agent else None,
            user_id=req.onBehalfOf.subject if req.onBehalfOf else None,
            source_kb_id=source_kb_id,
            operation="search",
            policy_decision="allowed",
            decision_reason=decision_reason,
            citation_ids=[c.id for c in citations],
        )

        envelope = CitedResponseEnvelope(
            ok=True,
            operation="search",
            sourceKbId=source_kb_id,
            answer=None,
            passages=passages,
            citations=citations,
            freshness=self._agent_freshness(retrieved_at=retrieved_at),
            accessDecision=self._access_decision(),
            audit=audit,
            usage=self._agent_usage(t0, content, retrieval_count=len(passages)),
            toolCalls=self._agent_tool_calls(content),
            pageInfo={"nextCursor": None, "hasMore": False, "pageLimit": limit},
        )
        self._cache(request_id, envelope)
        tracing.trace(
            "engine.search.response",
            requestId=request_id,
            ok=True,
            passageCount=len(passages),
            citationCount=len(citations),
        )
        return envelope

    async def ask(self, req: A2KRequest) -> CitedResponseEnvelope:
        """See search()'s own docstring for why this branches on
        config.is_mock the same way."""
        if config.is_mock:
            return await self._ask_deterministic(req)
        return await self._ask_via_agent(req)

    async def _ask_deterministic(self, req: A2KRequest) -> CitedResponseEnvelope:
        request_id = self._request_id(req)
        sources = req.sources or list(self.adapters)
        source_kb_id = self._source_kb_id(sources)
        t0 = time.monotonic()
        tracing.trace("engine.ask.request", requestId=request_id, query=req.query, sources=sources)

        try:
            facts_by_source = await self._gather_facts(req.query, sources, limit=50)
        except A2KError as err:
            return self._error_envelope("ask", source_kb_id, err, request_id)

        all_facts = [f for facts in facts_by_source.values() for f in facts]
        if not all_facts:
            return self._insufficient_evidence(source_kb_id, request_id, req)

        groups = synthesis.group_facts(all_facts)
        grouped_claims, citations = synthesis.build_claims_and_citations(groups)
        strict = req.requirements.strictGrounding
        answer, ungrounded = synthesis.synthesize_answer(grouped_claims, strict=strict)
        grounded_ratio = synthesis.compute_grounded_ratio(answer, ungrounded)
        strict_satisfied = len(ungrounded) == 0

        if strict and not strict_satisfied:
            # Shouldn't happen -- strict mode drops every ungrounded connector
            # at synthesis time -- but fail loudly rather than silently serve
            # an unsupported assertion (A2K-KCP section 8.1, option 4).
            err = A2KError(
                ErrorCode.GROUNDING_VIOLATION,
                "Strict grounding was requested but not satisfied.",
                details={"groundedRatio": grounded_ratio},
            )
            return self._error_envelope("ask", source_kb_id, err, request_id)

        claims = [c for _, group_claims in grouped_claims for c in group_claims]
        aware_conflicts, report_entries = conflict.build_aware_conflicts(grouped_claims)

        conflict_report = None
        if req.requirements.reportConflicts and report_entries:
            conflict_report = conflict.build_conflict_report(
                query=req.query,
                gateway_agent_id=self.gateway_kb_id,
                subject=req.onBehalfOf.subject if req.onBehalfOf else None,
                kbs_queried=[self.adapters[s].kb_id for s in sources],
                response_ref=request_id,
                report_entries=report_entries,
                regulated_mode=req.requirements.regulatedMode,
            )

        freshness = self._freshness(all_facts, req.requirements.maxStalenessHours)
        audit = gw_audit.write_audit(
            request_id=request_id,
            session_id=req.requestMetadata.sessionId if req.requestMetadata else None,
            agent_id=req.agent.agentId if req.agent else None,
            user_id=req.onBehalfOf.subject if req.onBehalfOf else None,
            source_kb_id=source_kb_id,
            operation="ask",
            policy_decision="allowed",
            decision_reason=None,
            citation_ids=[c.id for c in citations],
        )

        envelope = CitedResponseEnvelope(
            ok=True,
            operation="ask",
            sourceKbId=source_kb_id,
            answer=answer,
            claims=claims,
            citations=citations,
            grounding=Grounding(
                groundedRatio=grounded_ratio,
                ungroundedSpans=ungrounded,
                strictGroundingSatisfied=strict_satisfied,
            ),
            freshness=freshness,
            accessDecision=self._access_decision(),
            audit=audit,
            conflicts=aware_conflicts,
            usage=Usage(latencyMs=self._elapsed_ms(t0), retrievalCount=len(all_facts)),
            conflictReport=conflict_report,
        )
        self._cache(request_id, envelope)
        tracing.trace(
            "engine.ask.response",
            requestId=request_id,
            ok=True,
            claimCount=len(claims),
            citationCount=len(citations),
            groundedRatio=grounded_ratio,
        )
        return envelope

    async def _ask_via_agent(self, req: A2KRequest) -> CitedResponseEnvelope:
        """Live-mode path (mcp_to_agent_to_mcp branch): delegates to the agent
        (direct vendor-MCP discovery, self-reported grounding/conflicts)
        instead of the deterministic _gather_facts()/synthesis.py/conflict.py
        path above -- see _call_agent()'s docstring for the full trade-off.
        `conflictReport` (the full artifact, as opposed to the terse
        conflicts[] below) isn't rebuilt on this path -- accurately
        reconstructing it would need the same per-KB response bookkeeping
        conflict.py's deterministic comparison already has, which this path
        doesn't produce.

        The cache (a2k/cache/, config.cache_mode):
        - serve: retrieval runs first, within the D15 time budget; a hit is
          served and the vendor is never called. A miss, an error or the time
          budget running out goes to the vendor, as without a cache.
        - shadow: retrieval runs in parallel with the vendor call, so it adds
          no latency; the user always gets the vendor answer and the serving
          log records what the cache would have served.
        In both modes a vendor answer then goes to admission, in the
        background -- unless the lookup found an equivalent cached entry: in
        shadow mode (or a domain still in shadow) the vendor is called even
        on a hit, and admitting its answer would only add a duplicate the
        serve mode never creates (two ACTIVE entries for POCv3 ids 1 and 38
        after the shadow run of 2026-10-06). The cache is vendor-blind:
        `sources` plays no part in matching."""
        request_id = self._request_id(req)
        sources = req.sources or list(self.adapters)
        source_kb_id = self._source_kb_id(sources)
        t0 = time.monotonic()
        tracing.trace("engine.ask.request", requestId=request_id, query=req.query, sources=sources)

        session_id = req.requestMetadata.sessionId if req.requestMetadata else None
        lookup: retrieval.RetrievalResult | None = None
        shadow_task: asyncio.Task | None = None
        if config.cache_serving:
            lookup = await self._cache_retrieve(req.query, session_id, budget=config.cache_time_budget_seconds)
        elif config.cache_ready:
            shadow_task = asyncio.create_task(self._cache_retrieve(req.query, session_id, budget=None))

        # A hit is only served if its domain is in serve mode itself; a domain
        # still in shadow gets the vendor answer, the hit stays in the log.
        hit = lookup.hit if lookup and lookup.domains and config.domain_mode(lookup.domains[0]) == "serve" else None
        valid_as_of = None
        if hit is not None:
            content = self._content_from_cache_hit(hit)
            retrieved_at = hit.admitted_at.strftime("%Y-%m-%dT%H:%M:%SZ") if hit.admitted_at else None
            valid_as_of = hit.data_as_of.isoformat() if hit.data_as_of else None
            decision_reason = f"served from cache entry {hit.entry_id}"
        else:
            try:
                content = await self._call_agent("ask", req.query, req.sources, request_id)
            except A2KError as err:
                if shadow_task is not None:
                    self._log_retrieval(await shadow_task, request_id=request_id, session_id=session_id, served_from="vendor_error", vendor_entry_id=None)
                return self._error_envelope("ask", source_kb_id, err, request_id)
            retrieved_at = None
            decision_reason = None

        citations = [
            self._citation_from_agent(i, c, retrieved_at=retrieved_at)
            for i, c in enumerate(content.get("citations") or [])
        ]
        claims = [
            Claim(
                id=f"claim-{i + 1}",
                text=c.get("text", ""),
                type=c.get("type"),
                status=c.get("status") or "SUPPORTED",
                citationIds=self._citation_ids_for(citations, c.get("citationIndexes") or []),
                conflictsWith=[],
            )
            for i, c in enumerate(content.get("claims") or [])
        ]

        if not claims and not content.get("answer"):
            return self._insufficient_evidence(source_kb_id, request_id, req)

        grounded_ratio = float(content.get("groundedRatio") or 0.0)
        strict = req.requirements.strictGrounding
        # "Satisfied" here is the model's own self-report, not a verified
        # measurement -- gateway/synthesis.py's deterministic path only ever
        # calls this satisfied at an exact 1.0 (every span traced to a verbatim
        # citation quote); kept at the same threshold here for consistency,
        # even though this path can no longer *guarantee* it the same way.
        strict_satisfied = grounded_ratio >= 1.0
        if strict and not strict_satisfied:
            err = A2KError(
                ErrorCode.GROUNDING_VIOLATION,
                "Strict grounding was requested but not satisfied (self-reported groundedRatio "
                f"{grounded_ratio} < 1.0).",
                details={"groundedRatio": grounded_ratio},
            )
            return self._error_envelope("ask", source_kb_id, err, request_id)

        aware_conflicts = self._aware_conflicts_from_agent(content.get("conflicts") or [], claims)

        audit = gw_audit.write_audit(
            request_id=request_id,
            session_id=req.requestMetadata.sessionId if req.requestMetadata else None,
            agent_id=req.agent.agentId if req.agent else None,
            user_id=req.onBehalfOf.subject if req.onBehalfOf else None,
            source_kb_id=source_kb_id,
            operation="ask",
            policy_decision="allowed",
            decision_reason=decision_reason,
            citation_ids=[c.id for c in citations],
        )

        envelope = CitedResponseEnvelope(
            ok=True,
            operation="ask",
            sourceKbId=source_kb_id,
            answer=content.get("answer"),
            claims=claims,
            citations=citations,
            grounding=Grounding(
                groundedRatio=grounded_ratio,
                ungroundedSpans=[],
                confidence=grounded_ratio,
                confidenceMethod="llm-self-report",
                strictGroundingSatisfied=strict_satisfied,
            ),
            freshness=self._agent_freshness(retrieved_at=retrieved_at, valid_as_of=valid_as_of),
            accessDecision=self._access_decision(),
            audit=audit,
            conflicts=aware_conflicts,
            usage=self._agent_usage(t0, content, retrieval_count=len(claims)),
            toolCalls=self._agent_tool_calls(content),
        )
        self._cache(request_id, envelope)
        if shadow_task is not None:
            lookup = await shadow_task  # started with the vendor call; long done by now
        # What serve mode would have served (`hit` is what was served). The
        # session turn points at it too, so a reformulation deprecates the
        # cached entry exactly as it would in serve mode.
        would_hit = lookup.hit if lookup is not None else None
        vendor_entry_id = self._submit_admission(req.query, content) if would_hit is None else None
        self._record_turn(session_id, req.query, would_hit.entry_id if would_hit is not None else vendor_entry_id)
        if lookup is not None:
            self._log_retrieval(
                lookup,
                request_id=request_id,
                session_id=session_id,
                served_from="cache" if hit is not None else "vendor",
                vendor_entry_id=vendor_entry_id,
            )
        tracing.trace(
            "engine.ask.response",
            requestId=request_id,
            ok=True,
            claimCount=len(claims),
            citationCount=len(citations),
            groundedRatio=grounded_ratio,
        )
        return envelope

    async def explain(self, req: ExplainRequest) -> CitedResponseEnvelope:
        request_id = self._request_id(req)
        prior = self._response_cache.get(req.answerRef) if req.answerRef else None
        if prior is None:
            err = A2KError(
                ErrorCode.NOT_FOUND,
                f"No cached response found for answerRef={req.answerRef!r}. "
                "explain() is stateful in this gateway: pass the requestId returned by a prior ask/search.",
            )
            return self._error_envelope("explain", self.gateway_kb_id, err, request_id)

        claim_ids = set(req.claimIds) or {c.id for c in prior.claims}
        explained_claims = [c for c in prior.claims if c.id in claim_ids]
        if not explained_claims:
            err = A2KError(ErrorCode.NOT_FOUND, "None of the requested claimIds exist on the referenced answer.")
            return self._error_envelope("explain", prior.sourceKbId, err, request_id)

        explained_citation_ids = {cid for c in explained_claims for cid in c.citationIds}
        explained_citations = [c for c in prior.citations if c.id in explained_citation_ids]

        answer, ungrounded = self._build_explanation(explained_claims, explained_citations)
        grounded_ratio = synthesis.compute_grounded_ratio(answer, ungrounded)

        audit = gw_audit.write_audit(
            request_id=request_id,
            session_id=req.requestMetadata.sessionId if req.requestMetadata else None,
            agent_id=None,
            user_id=req.onBehalfOf.subject if req.onBehalfOf else None,
            source_kb_id=prior.sourceKbId,
            operation="explain",
            policy_decision="allowed",
            decision_reason=None,
            citation_ids=[c.id for c in explained_citations],
        )

        envelope = CitedResponseEnvelope(
            ok=True,
            operation="explain",
            sourceKbId=prior.sourceKbId,
            answer=answer,
            claims=explained_claims,
            citations=explained_citations,
            grounding=Grounding(
                groundedRatio=grounded_ratio,
                ungroundedSpans=ungrounded,
                strictGroundingSatisfied=len(ungrounded) == 0,
            ),
            freshness=prior.freshness,
            accessDecision=self._access_decision(),
            audit=audit,
        )
        return envelope

    async def get_document(self, req: GetDocumentRequest) -> GetDocumentResponse:
        request_id = self._request_id(req)
        adapter = next((a for a in self.adapters.values() if req.documentId.startswith(a.kb_id)), None)

        if adapter is None:
            err = A2KError(ErrorCode.NOT_FOUND, f"Unknown document namespace for documentId={req.documentId!r}.")
            return self._get_document_error(self.gateway_kb_id, err, request_id, req)

        try:
            doc = await adapter.get_document(req.documentId)
        except A2KError as err:
            return self._get_document_error(adapter.kb_id, err, request_id, req)

        if doc is None:
            err = A2KError(ErrorCode.NOT_FOUND, f"Document {req.documentId!r} not found.")
            return self._get_document_error(adapter.kb_id, err, request_id, req)

        gw_audit.write_audit(
            request_id=request_id,
            session_id=req.requestMetadata.sessionId if req.requestMetadata else None,
            agent_id=None,
            user_id=req.onBehalfOf.subject if req.onBehalfOf else None,
            source_kb_id=adapter.kb_id,
            operation="getDocument",
            policy_decision="allowed",
            decision_reason=None,
            citation_ids=[],
        )
        return GetDocumentResponse(
            ok=True,
            sourceKbId=adapter.kb_id,
            document=Document(
                documentId=doc.document_id,
                title=doc.title,
                sourceUrl=doc.source_url,
                mimeType=doc.mime_type,
                content=doc.content,
                hash=doc.hash,
                lastUpdated=doc.last_updated,
                classification=doc.classification,
            ),
            accessDecision=self._access_decision(),
        )

    def get_cached(self, request_id: str) -> CitedResponseEnvelope | None:
        """Public accessor for transports that need the raw prior envelope
        (e.g. MCP's a2k.reportConflict) rather than a re-derived explanation."""
        return self._response_cache.get(request_id)

    # -- helpers ------------------------------------------------------------

    async def _gather_facts(self, query: str, sources: list[str], *, limit: int) -> dict[str, list[Fact]]:
        """Mock-mode-only now (see search()/ask()'s config.is_mock dispatch)
        -- live traffic goes through _call_agent() instead. Kept as-is so the
        existing mock-mode test suite keeps exercising exactly the same
        deterministic Fact-gathering it always has."""

        async def run(name: str) -> tuple[str, list[Fact]]:
            return name, await self.adapters[name].search(query, limit=limit)

        results = await asyncio.gather(*(run(s) for s in sources))
        return dict(results)

    async def _cache_retrieve(self, query: str, session_id: str | None, *, budget: float | None) -> retrieval.RetrievalResult:
        """Retrieval in a worker thread (its Bedrock and Data API calls are
        blocking). With a `budget` (serve mode, D15) a slow lookup counts as a
        miss and the request goes to the vendor; the thread is left to finish
        on its own. Never raises."""
        call = asyncio.to_thread(self._cache_lookup_sync, query, session_id)
        try:
            return await (asyncio.wait_for(call, timeout=budget) if budget else call)
        except asyncio.TimeoutError:
            return retrieval.RetrievalResult(error=f"time budget exceeded ({budget}s, D15)")
        except Exception as exc:  # noqa: BLE001 -- the cache must never break a request
            return retrieval.RetrievalResult(error=f"{type(exc).__name__}: {exc}")

    def _cache_lookup_sync(self, query: str, session_id: str | None = None) -> retrieval.RetrievalResult:
        try:
            bedrock_client, store, _, _, _ = _cache_clients()
        except Exception as exc:  # noqa: BLE001
            return retrieval.RetrievalResult(error=f"clients: {type(exc).__name__}: {exc}")
        return retrieval.lookup(query, bedrock_client=bedrock_client, store=store, settings=_retrieval_settings(), session_id=session_id)

    def _content_from_cache_hit(self, hit: retrieval.CacheHit) -> dict:
        """The agent's own content shape, so the envelope is built by the same
        code as a live answer. groundedRatio 1.0: the entry was admitted only
        after the judge found every requested element covered, with quotes
        verified against the raw tool output (a2k/cache/decision.py)."""
        return {
            "answer": hit.answer,
            "claims": [],
            "citations": [
                {"title": s.get("title"), "sourceUrl": s.get("sourceUrl"), "documentId": s.get("documentId")}
                for s in hit.sources
            ],
            "groundedRatio": 1.0,
            "conflicts": [],
            "toolCalls": [],
        }

    def _log_retrieval(
        self,
        lookup: retrieval.RetrievalResult,
        *,
        request_id: str,
        session_id: str | None,
        served_from: str,
        vendor_entry_id: str | None,
    ) -> None:
        """The serving log (proposal Sheet 6): one JSON line per ask with the
        cache on. In shadow mode `would_hit` is what the cache would have
        served; `vendor_entry_id` is the admission candidate created from the
        vendor answer, whose evidence bundle (S3) holds that answer -- so a
        would-be hit can be compared with the fresh vendor answer afterwards."""
        hit = lookup.hit
        record = {
            "event": "cache.retrieval",
            "mode": config.cache_mode,
            "request_id": request_id,
            "session_id": session_id,
            # caller (K2's sessionId) or derived from a transport header (see
            # mcp_server/server.py _session_id) -- filter on "caller" for D14.
            "session_source": (session_id.split(":", 1)[0] if session_id.startswith(("mcp:", "agentcore:")) else "caller") if session_id else None,
            "served_from": served_from,
            "would_hit": hit is not None,
            "entry_id": hit.entry_id if hit else None,
            "cosine": round(hit.cosine, 4) if hit else None,
            "served_version": hit.served_version if hit else None,
            "threshold": lookup.threshold,
            "domains": lookup.domains,
            "original_lang": lookup.original_lang,
            "question_en": lookup.question_en,
            "domain_mode": config.domain_mode(lookup.domains[0]) if lookup.domains else None,
            "reformulation": lookup.reformulation,
            "candidates": lookup.candidates,
            "checks": lookup.checks,
            "skipped": lookup.skipped,
            "error": lookup.error,
            "timings_ms": lookup.timings_ms,
            "total_ms": lookup.total_ms,
            "within_budget": lookup.total_ms <= config.cache_time_budget_seconds * 1000,
            "vendor_entry_id": vendor_entry_id,
        }
        print(json.dumps(record, ensure_ascii=False, default=str), file=sys.stdout, flush=True)

    def _record_turn(self, session_id: str | None, query: str, served_entry_id: str | None) -> None:
        """Session memory for the next turn's reformulation check: the question
        as asked and the entry the user got -- the cache hit, or the admission
        candidate made from the vendor answer (None if the answer is not a
        candidate; a reformulation of it still counts for the re-ask rate).
        In the background, after the answer; no session id, nothing stored."""
        if not config.cache_ready or not session_id:
            return

        def _write() -> None:
            try:
                _, _, store, _, _ = _cache_clients()
                store.record_turn(session_id, query, served_entry_id, window_seconds=config.cache_reformulation_window_seconds)
            except Exception as exc:  # noqa: BLE001 -- losing one turn only skips one reformulation check
                print(f"a2k-box: cache session turn not recorded: {type(exc).__name__}: {exc}", file=sys.stdout, flush=True)

        admission.run_in_background(_write, workers=config.admission_workers)

    def _submit_admission(self, query: str, content: dict) -> str | None:
        """Hands a live vendor answer to the cache admission pipeline
        (a2k/cache/admission.py), which runs in the background -- the
        response has already been built and is returned right after this.

        Only `ask` answers from a single vendor in the allowed list (D11) are
        candidates: a fan-out answer mixes vendors whose contracts may differ,
        and an answer with no vendor tool call has nothing to judge it against.
        Never raises: building the candidate is pure, and everything that
        touches AWS happens inside the worker. Returns the candidate's entry_id,
        or None when the answer is not a candidate."""
        if not config.cache_ready:
            return None
        try:
            vendors = {tc.get("vendor") for tc in (content.get("toolCalls") or []) if tc.get("vendor")}
            if len(vendors) != 1 or next(iter(vendors)) not in config.admission_allowed_vendors:
                return None
            candidate = Candidate(
                entry_id=str(uuid.uuid4()),
                question=query,
                answer=content.get("answer") or "",
                vendor=next(iter(vendors)),
                tool_calls=list(content.get("toolCalls") or []),
                citations=list(content.get("citations") or []),
                created_at=datetime.now(timezone.utc),
            )
            admission.submit(candidate, settings=_admission_settings(), deps=_admission_deps)
            return candidate.entry_id
        except Exception as exc:  # noqa: BLE001 -- admission must never break a live request
            print(f"a2k-box: cache admission submit failed: {type(exc).__name__}: {exc}", file=sys.stdout, flush=True)
            return None

    async def _get_agent_token(self) -> str:
        """Cognito client-credentials token for the agent's own inbound-auth
        pool -- separate from anything the Cala/Sayari adapters use. Mirrors
        agent/core.py's get_bearer_token() (same client-credentials shape,
        different pool/URL) -- see config.py's agent_pool_* fields."""
        if self._agent_token and time.monotonic() < self._agent_token_expires_at:
            return self._agent_token

        data = {"grant_type": "client_credentials"}
        if config.agent_pool_scope:
            data["scope"] = config.agent_pool_scope

        async with httpx.AsyncClient(timeout=30, verify=config.httpx_verify) as client:
            try:
                resp = await client.post(
                    config.agent_pool_token_url,
                    auth=(config.agent_pool_client_id, config.agent_pool_client_secret),
                    headers={"Content-Type": "application/x-www-form-urlencoded"},
                    data=data,
                )
                resp.raise_for_status()
                payload = resp.json()
            except httpx.HTTPError as exc:
                raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Agent pool token request failed: {exc}") from exc

        self._agent_token = payload["access_token"]
        self._agent_token_expires_at = time.monotonic() + float(payload.get("expires_in", 3600)) - 30
        return self._agent_token

    async def _call_agent(self, operation: str, query: str, sources: list[str] | None, request_id: str) -> dict:
        """Invokes the agent's Runtime directly over HTTPS (Cognito Bearer
        token -- see _get_agent_token()), passing the vendor catalogue this
        gateway already has loaded (vendor_catalogue()) so the agent doesn't
        re-fetch it. See agent/entrypoint_a2k.py for the exact payload/
        response contract this call makes.

        `request_id` is this gateway's own requestId (see _request_id()) --
        passed through as `requestId` in the payload so entrypoint_a2k.py can
        use it as the `session_id` on the agent's own observability.py logs/
        metrics, correlating them with this gateway's audit records
        (gw_audit.write_audit) for the same request, same idea as
        core.py's session_id threading.

        mcp_to_agent_to_mcp branch: this is the deliberate trade this branch
        makes -- the agent connects directly to Cala's/Sayari's own MCP
        servers and discovers their tools live (agent/vendor_mcp_client.py,
        agent/direct_agent.py) instead of this gateway's adapters
        (adapters/cala_mcp.py, adapters/sayari_mcp.py) calling them through a
        fixed, hardcoded sequence. What's gained: no per-vendor tool-call
        logic to maintain here, and the agent adapts if a vendor's own MCP
        tool set changes without a code change on this side. What's lost:
        gateway/synthesis.py's exact (not estimated) groundedRatio and
        gateway/conflict.py's deterministic cross-source comparison -- both
        become self-reported by the model instead (see ask()'s own comments).
        """
        if not config.agent_call_ready:
            raise A2KError(
                ErrorCode.UPSTREAM_ERROR,
                "AGENT_POOL_CLIENT_ID/AGENT_POOL_CLIENT_SECRET/AGENT_POOL_TOKEN_URL/"
                "AGENT_RUNTIME_URL are not all set -- cannot reach the agent Runtime.",
                retryable=False,
            )

        token = await self._get_agent_token()
        payload = {
            "operation": operation,
            "query": query,
            "sources": sources,
            "catalogue": vendor_catalogue(),
            "requestId": request_id,
        }

        # 240s, not 120s: ground_truth_v4.csv's live run (2026-09-08) showed 5/12
        # in-scope (Sayari ownership/sanctions) queries hitting a flat 120s
        # ceiling with no data back at all -- these are the multi-entity,
        # multi-registry cross-check questions (e.g. matching a UK company
        # against Companies House *and* OpenCorporates, resolving several name
        # variants each) where the agent's phase 2 genuinely needs more than
        # 120s of live tool-calling against Sayari's own MCP. This is a flat
        # POST (not the streaming MCP path a2k-box's own callers use), so
        # nothing here keeps the connection alive the way SSE ping frames do
        # on that side -- it really is all-or-nothing up to this ceiling.
        #
        # 600s, not 240s: the POCv3 Cala run (2026-09-29) timed out on 23 rows,
        # and the agent Runtime's own tool-call logs show those sessions kept
        # going after this side gave up -- 21 of the 23 would have finished
        # within 600s (the other two ran 623s and 882s). Cala's knowledge_search
        # alone takes 45-100s per call, plus 45-110s of model time writing the
        # answer, so a question needing 3+ searches can't fit in 240s.
        async with httpx.AsyncClient(timeout=600.0, verify=config.httpx_verify) as client:
            try:
                resp = await client.post(
                    config.agent_runtime_url,
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        "Accept": "application/json",
                    },
                )
                resp.raise_for_status()
                body = resp.json()
            except httpx.HTTPError as exc:
                raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Agent Runtime call failed: {exc}") from exc

        if not body.get("ok"):
            raise A2KError(ErrorCode.UPSTREAM_ERROR, f"Agent reported failure: {body.get('error')}")
        return body.get("content") or {}

    def _citation_from_agent(self, index: int, citation: dict, *, retrieved_at: str | None = None) -> Citation:
        """Builds a real Citation from the agent's lightweight CitationOut
        shape (agent/direct_agent.py) -- `id`/`retrievedAt` are assigned here,
        not trusted from the model, same division of labor as
        gateway/synthesis.py's Fact -> Citation conversion on the
        Gateway-mediated path. No sourceHash: unlike that path, nothing here
        re-fetches the source document to hash it.

        `retrieved_at` defaults to now() (a live call, fetched this instant)
        but a cache hit (a2k/cache/retrieval.py) passes the entry's admission
        time instead -- same "never claim now for a replayed answer" reasoning
        as _agent_freshness()'s own `retrieved_at` param."""
        return Citation(
            id=f"citation-{index + 1}",
            documentId=citation.get("documentId"),
            title=citation.get("title"),
            sourceUrl=citation.get("sourceUrl"),
            retrievedAt=retrieved_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )

    def _citation_ids_for(self, citations: list[Citation], indexes: list[int]) -> list[str]:
        return [citations[i].id for i in indexes if 0 <= i < len(citations)]

    def _agent_usage(self, t0: float, content: dict, *, retrieval_count: int) -> Usage:
        """`content["usage"]` comes from direct_agent.py's handle() (summed
        across every vendor phase 2 actually queried) -- absent/malformed on
        anything that monkeypatches _call_agent without it (existing tests),
        so every lookup here is defensive rather than assuming the shape."""
        agent_usage = content.get("usage") or {}
        return Usage(
            latencyMs=self._elapsed_ms(t0),
            retrievalCount=retrieval_count,
            inputTokens=agent_usage.get("inputTokens"),
            outputTokens=agent_usage.get("outputTokens"),
            totalTokens=agent_usage.get("totalTokens"),
        )

    def _agent_tool_calls(self, content: dict) -> list[ToolCallRecord]:
        return [
            ToolCallRecord(
                vendor=tc.get("vendor", ""),
                toolName=tc.get("toolName", ""),
                input=tc.get("input") or {},
                output=tc.get("output"),
                status=tc.get("status", "success"),
                errorType=tc.get("errorType"),
            )
            for tc in (content.get("toolCalls") or [])
        ]

    def _agent_freshness(self, retrieved_at: str | None = None, valid_as_of: str | None = None) -> Freshness:
        """The agent doesn't report source-level update timestamps, so on a
        live call (retrieved_at omitted) there is nothing to compute `stale`
        from: always False, retrieved now.

        A cache hit passes the entry's admitted_at as `retrieved_at` -- a
        replayed answer must never claim it was fetched just now -- and the
        date the data refers to as `valid_as_of`. Still never stale: retrieval
        only returns entries before their expires_at (a2k/cache/expiry.py)."""
        now = datetime.now(timezone.utc)
        return Freshness(
            sourceLastUpdated=None,
            reviewedAt=None,
            retrievedAt=retrieved_at or now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            stale=False,
            validAsOf=valid_as_of or (retrieved_at or now.strftime("%Y-%m-%dT%H:%M:%SZ"))[:10],
        )

    def _aware_conflicts_from_agent(self, agent_conflicts: list[dict], claims: list[Claim]) -> list[AwareConflict]:
        """Maps direct_agent.py's self-reported ConflictOut list into real
        AwareConflict objects. `nature` is validated against ConflictType's
        allowed values (falling back to "unknown") since it's LLM free text,
        not a value gateway/conflict.py's deterministic comparison already
        constrained the way it does on the Gateway-mediated path."""
        result: list[AwareConflict] = []
        for i, c in enumerate(agent_conflicts):
            this_idx, other_idx = c.get("thisClaimIndex"), c.get("otherClaimIndex")
            if this_idx is None or other_idx is None:
                continue
            if not (0 <= this_idx < len(claims) and 0 <= other_idx < len(claims)):
                continue
            nature = c.get("nature") if c.get("nature") in _VALID_CONFLICT_TYPES else "unknown"
            result.append(
                AwareConflict(
                    id=f"conflict-{i + 1}",
                    claimId=claims[this_idx].id,
                    nature=nature,
                    thisPosition=claims[this_idx].text,
                    otherPosition=claims[other_idx].text,
                    otherSource=AwareConflictSource(kbId=self.gateway_kb_id),
                    assessment=c.get("assessment") or "",
                    rationale=c.get("rationale") or "",
                )
            )
        return result

    def _source_kb_id(self, sources: list[str]) -> str:
        if len(sources) == 1:
            adapter = self.adapters.get(sources[0])
            if adapter is not None:
                return adapter.kb_id
            # No deterministic adapter for this vendor (e.g. Linkup -- agent-
            # mediated only, see vendor_mcp_client.py) -- same "urn:a2k:vendor:
            # <sourceId>" convention adapters/*.py's own KB_ID constants use
            # and a2k/cards/*.json's own `id` field already follows, just
            # computed here instead of round-tripping through a card lookup.
            return f"urn:a2k:vendor:{sources[0]}"
        return self.gateway_kb_id

    def _access_decision(self) -> AccessDecision:
        return AccessDecision(
            decision="allowed",
            reason="Vendor commercial/public data (tier S0, A2K-KBCard-Schema section 4.4.1); no OBO assertion required at this tier.",
            appliedScopes=[],
            dataClassification="public",
            policyEngine="a2k-box-gateway",
            decisionId=str(uuid.uuid4()),
        )

    def _freshness(self, facts: list[Fact], max_staleness_hours: int | None) -> Freshness:
        now = datetime.now(timezone.utc)
        retrieved_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        last_updates = sorted(f.source_last_updated for f in facts if f.source_last_updated)
        oldest = last_updates[0] if last_updates else None
        newest = last_updates[-1] if last_updates else None

        stale = False
        if max_staleness_hours is not None and oldest:
            oldest_dt = datetime.fromisoformat(oldest.replace("Z", "+00:00"))
            stale = (now - oldest_dt) > timedelta(hours=max_staleness_hours)

        return Freshness(
            sourceLastUpdated=oldest,
            reviewedAt=newest,
            retrievedAt=retrieved_at,
            stale=stale,
            validAsOf=now.strftime("%Y-%m-%d"),
        )

    def _build_explanation(
        self, claims: list[Claim], citations: list[Citation]
    ) -> tuple[str, list]:
        status_notes = {
            "SUPPORTED": "supported by",
            "DISPUTED": "disputed -- it reflects only one side of a cross-source disagreement, sourced from",
            "INSUFFICIENT_EVIDENCE": "not backed by sufficient evidence; nominally sourced from",
        }
        segments: list[tuple[str, bool]] = []
        for i, claim in enumerate(claims):
            if i > 0:
                segments.append(("\n\n", True))
            claim_citations = [c for c in citations if c.id in claim.citationIds]
            sources_desc = "; ".join(
                f"{c.title} ({c.sourceUrl})" if c.sourceUrl else (c.title or c.documentId or c.id)
                for c in claim_citations
            ) or "no citation on file"
            note = status_notes.get(claim.status, f"status {claim.status}, sourced from")
            segments.append(('"', False))
            segments.append((claim.text, True))
            segments.append(('"', False))
            segments.append((f" is {note} {sources_desc}.", False))
        return synthesis.assemble_segments(segments)

    def _cache(self, request_id: str, envelope: CitedResponseEnvelope) -> None:
        self._response_cache[request_id] = envelope
        while len(self._response_cache) > _CACHE_MAX_SIZE:
            del self._response_cache[next(iter(self._response_cache))]

    def _request_id(self, req) -> str:
        meta = getattr(req, "requestMetadata", None)
        return (meta.requestId if meta else None) or str(uuid.uuid4())

    def _elapsed_ms(self, t0: float) -> int:
        return int((time.monotonic() - t0) * 1000)

    def _error_envelope(
        self, operation: str, source_kb_id: str, err: A2KError, request_id: str
    ) -> CitedResponseEnvelope:
        gw_audit.write_audit(
            request_id=request_id,
            session_id=None,
            agent_id=None,
            user_id=None,
            source_kb_id=source_kb_id,
            operation=operation,
            policy_decision="error",
            decision_reason=err.message,
            citation_ids=[],
        )
        return CitedResponseEnvelope(
            ok=False,
            operation=operation,
            sourceKbId=source_kb_id,
            error=ErrorObject(**err.to_dict()),
        )

    def _get_document_error(
        self, source_kb_id: str, err: A2KError, request_id: str, req: GetDocumentRequest
    ) -> GetDocumentResponse:
        gw_audit.write_audit(
            request_id=request_id,
            session_id=req.requestMetadata.sessionId if req.requestMetadata else None,
            agent_id=None,
            user_id=req.onBehalfOf.subject if req.onBehalfOf else None,
            source_kb_id=source_kb_id,
            operation="getDocument",
            policy_decision="error",
            decision_reason=err.message,
            citation_ids=[],
        )
        return GetDocumentResponse(ok=False, sourceKbId=source_kb_id, error=ErrorObject(**err.to_dict()))

    def _insufficient_evidence(
        self, source_kb_id: str, request_id: str, req: A2KRequest
    ) -> CitedResponseEnvelope:
        claim = Claim(
            id="claim-1",
            text=f"No data available for query: {req.query!r}.",
            type="unknown",
            status="INSUFFICIENT_EVIDENCE",
            citationIds=[],
            conflictsWith=[],
        )
        audit = gw_audit.write_audit(
            request_id=request_id,
            session_id=req.requestMetadata.sessionId if req.requestMetadata else None,
            agent_id=req.agent.agentId if req.agent else None,
            user_id=req.onBehalfOf.subject if req.onBehalfOf else None,
            source_kb_id=source_kb_id,
            operation="ask",
            policy_decision="allowed",
            decision_reason="No matching entity in any queried source.",
            citation_ids=[],
        )
        envelope = CitedResponseEnvelope(
            ok=True,
            operation="ask",
            sourceKbId=source_kb_id,
            answer=None,
            claims=[claim],
            grounding=Grounding(groundedRatio=0.0, ungroundedSpans=[], strictGroundingSatisfied=False),
            freshness=self._freshness([], req.requirements.maxStalenessHours),
            accessDecision=self._access_decision(),
            audit=audit,
        )
        return envelope


engine = GatewayEngine()
