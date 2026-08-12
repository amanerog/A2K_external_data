"""GatewayEngine: the one place that knows how to answer search/ask/explain/
getDocument. `api/rest.py` and `mcp_server/server.py` are thin transport
adapters over this class -- see plan decision "un unico motor, dos
transportes".
"""

from __future__ import annotations

import asyncio
import time
import uuid
from datetime import datetime, timedelta, timezone

from ..adapters.base import Fact, ProviderAdapter
from ..adapters.cala_mcp import CalaMcpAdapter
from ..adapters.sayari import SayariAdapter
from ..config import config
from ..errors import A2KError, ErrorCode
from ..models.envelope import (
    AccessDecision,
    Citation,
    CitedResponseEnvelope,
    Claim,
    Document,
    ErrorObject,
    Freshness,
    GetDocumentResponse,
    Grounding,
    Usage,
)
from ..models.request import A2KRequest, ExplainRequest, GetDocumentRequest
from . import audit as gw_audit
from . import conflict, synthesis

GATEWAY_KB_ID = "urn:a2k:gateway:k2-external-intel"
_CACHE_MAX_SIZE = 500


class GatewayEngine:
    def __init__(self) -> None:
        # Cala: MCP (Cala's own hosted server, adapters/cala_mcp.py) is the
        # default now -- Sayari still REST (adapters/sayari.py) until its
        # MCP endpoint/tools are confirmed.
        self.adapters: dict[str, ProviderAdapter] = {"cala": CalaMcpAdapter(), "sayari": SayariAdapter()}
        self.gateway_kb_id = GATEWAY_KB_ID
        self._response_cache: dict[str, CitedResponseEnvelope] = {}

    # -- operations -------------------------------------------------------

    async def search(self, req: A2KRequest) -> CitedResponseEnvelope:
        request_id = self._request_id(req)
        sources = req.sources or list(self.adapters)
        source_kb_id = self._source_kb_id(sources)
        limit = req.pagination.limit if req.pagination else 10
        t0 = time.monotonic()

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
        return envelope

    async def ask(self, req: A2KRequest) -> CitedResponseEnvelope:
        request_id = self._request_id(req)
        sources = req.sources or list(self.adapters)
        source_kb_id = self._source_kb_id(sources)
        t0 = time.monotonic()

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
        async def run(name: str) -> tuple[str, list[Fact]]:
            return name, await self.adapters[name].search(query, limit=limit)

        results = await asyncio.gather(*(run(s) for s in sources))
        return dict(results)

    def _source_kb_id(self, sources: list[str]) -> str:
        if len(sources) == 1:
            return self.adapters[sources[0]].kb_id
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
