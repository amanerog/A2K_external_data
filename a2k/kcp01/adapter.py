"""A2K-KCP 0.1 ask/search over the internal engine.

Request side: the 0.1 request is validated with the reference models, the
0.1 rules this KB can settle before answering are applied (version, strict
grounding, point in time, purpose, cursor, classification and lifecycle
filters), and what remains is mapped onto the internal A2KRequest.

Response side:
- claims are the agent's own, written verbatim from its answer and located
  in it here (their answerSpans); each cites only usable citations (with a
  sourceUrl that is not a vendor's API: getDocument is not advertised, so a
  citation with no URL has no permitted resolver). When none can be located
  -- or on a cache hit, which keeps no claims -- one claim spans the whole
  answer;
- grounding is the agent's self-report: method llm-self-report, so status is
  at most `supported`, never `strict-satisfied` (section 5.6);
- the agent's coverage sets the outcome: complete -> answered, partial ->
  partial with a refusal and what is missing as warnings, none -> refused;
- non-answers are successes with an outcome and a refusal, never errors;
  the vendor-unavailable fallback is an UPSTREAM_ERROR failure;
- error messages are generic per code: internal messages can name vendors
  or runtimes, which the caller is not to see.

requirements.maxEvidenceAgeSeconds (decided 2026-10-08): it is measured
against sourceVerifiedAt -- when the origin confirmed the evidence -- and
evidence without it cannot qualify (section 5.5). Nothing here has one: the
vendors answer from their own stores and the cache is a copy, neither a
confirmation against the origin. So a request carrying it is refused
(EVIDENCE_TOO_OLD) for ask and gets an empty result for search, whatever the
limit. Routing such requests to a live fetch of the origin (Linkup's
linkup-fetch) is on hold.

Not mapped: onBehalfOf (auth.mode service: no verified human subject, the
member is at most a hint) and filters.language (answers follow the language
of the question). Unknown members are rejected: the MCP layer hands the
arguments over exactly as sent (mcp_server/server.py _KcpFastMCP).
"""

from __future__ import annotations

import re
import uuid
from urllib.parse import urlparse
from datetime import datetime, timezone
from typing import Any

from a2k_kbcard.models.envelope import Failure
from a2k_kcp.models.requests import AskRequest, SearchRequest
from a2k_kcp.models.responses import AskSuccess, SearchSuccess
from pydantic import ValidationError

from ..config import config
from ..gateway.engine import engine
from ..models.envelope import CitedResponseEnvelope
from ..models.request import A2KRequest, Pagination, RequestContext, RequestMetadata, Requirements
from . import card

VERSION = "0.1"

_URI = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:[^\s]+$")
_TIMESTAMP = re.compile(r"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(\.\d+)?Z$")

# Caller-safe messages, one per code (section 2.3: nothing names the target).
_MESSAGES = {
    "INVALID_REQUEST": "The request is not valid.",
    "SCHEMA_VALIDATION_FAILED": "The request does not conform to the A2K 0.1 schema.",
    "UNSUPPORTED_VERSION": "This A2K contract version is not supported.",
    "UNSUPPORTED_OPERATION": "This operation is not supported.",
    "REQUIRED_FEATURE_UNSUPPORTED": "A required feature is not supported.",
    "AUTHENTICATION_REQUIRED": "Authentication is required.",
    "AUTHORIZATION_FAILED": "Authorization failed.",
    "ACCESS_DENIED": "Access denied.",
    "NOT_FOUND": "Not found.",
    "PAGINATION_CURSOR_INVALID": "The pagination cursor is not valid.",
    "RATE_LIMITED": "Too many requests; try again later.",
    "TIMEOUT": "The request timed out; try again later.",
    "UPSTREAM_ERROR": "The data provider is temporarily unavailable; try again later.",
    "INTERNAL_ERROR": "An internal error occurred.",
}

# Internal (pre-0.1) error codes that are substantive non-answers in 0.1:
# an `ask` outcome with a refusal, never an error (section 4.2 rule 4).
_REFUSALS = {
    "OUT_OF_SCOPE": "OUT_OF_SCOPE",
    "AMBIGUOUS_QUERY": "AMBIGUOUS_QUERY",
    "INSUFFICIENT_EVIDENCE": "INSUFFICIENT_EVIDENCE",
    "STALE_SOURCE": "EVIDENCE_TOO_OLD",
    "GROUNDING_VIOLATION": "GROUNDING_VIOLATION",
    "CONFLICT_DETECTED": "CONFLICT_UNRESOLVED",
    "HUMAN_REVIEW_REQUIRED": "HUMAN_REVIEW_REQUIRED",
}
# The fixed outcome <-> refusal-code pairings (section 4.2 rule 3).
_OUTCOME_OF = {"OUT_OF_SCOPE": "out-of-scope", "AMBIGUOUS_QUERY": "clarification-required", "HUMAN_REVIEW_REQUIRED": "human-review-required"}
# Internal error codes that keep their meaning; anything else is INTERNAL_ERROR.
_ERRORS = {
    "INVALID_REQUEST", "SCHEMA_VALIDATION_FAILED", "UNSUPPORTED_VERSION", "UNSUPPORTED_OPERATION",
    "AUTHENTICATION_REQUIRED", "AUTHORIZATION_FAILED", "ACCESS_DENIED", "NOT_FOUND",
    "PAGINATION_CURSOR_INVALID", "RATE_LIMITED", "TIMEOUT", "UPSTREAM_ERROR", "INTERNAL_ERROR",
}
_ERROR_ALIASES = {"OBO_ASSERTION_REQUIRED": "AUTHORIZATION_FAILED", "OBO_ASSERTION_INVALID": "AUTHORIZATION_FAILED"}
_RETRYABLE = {"RATE_LIMITED", "TIMEOUT", "UPSTREAM_ERROR"}

_GROUNDING_METHOD = "llm-self-report"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _response_id() -> str:
    return f"resp-{uuid.uuid4().hex}"


def _request_id(arguments: dict[str, Any]) -> str | None:
    """Echoed on a failure only when the request carried a valid one (section 2.3)."""
    value = arguments.get("requestId")
    return value if isinstance(value, str) and 1 <= len(value) <= 128 else None


# --- failures ------------------------------------------------------------------


def failure(operation: str, request_id: str | None, code: str, *, retryable: bool | None = None, details: dict | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "a2kVersion": VERSION,
        "ok": False,
        "operation": operation,
        "responseId": _response_id(),
        "generatedAt": _now(),
        "error": {"code": code, "message": _MESSAGES[code], "retryable": code in _RETRYABLE if retryable is None else retryable},
    }
    if request_id is not None:
        payload["requestId"] = request_id
    if details:
        payload["error"]["details"] = details
    return Failure.model_validate(payload).model_dump(mode="json", exclude_none=True)


def _from_internal_error(operation: str, request_id: str, env: CitedResponseEnvelope) -> dict[str, Any]:
    code = env.error.code if env.error else "INTERNAL_ERROR"
    code = _ERROR_ALIASES.get(code, code)
    return failure(operation, request_id, code if code in _ERRORS else "INTERNAL_ERROR")


# --- requests ------------------------------------------------------------------


def _parse(operation: str, model, arguments: dict[str, Any]):
    """The validated request, or the failure envelope to return instead."""
    request_id = _request_id(arguments)
    version = arguments.get("a2kVersion")
    if version is not None and version != VERSION:
        return None, failure(operation, request_id, "UNSUPPORTED_VERSION", details={"supportedVersions": [VERSION]})
    if "operation" in arguments and arguments["operation"] != operation:
        return None, failure(operation, request_id, "INVALID_REQUEST")
    if "operation" not in arguments or "a2kVersion" not in arguments:
        return None, failure(operation, request_id, "SCHEMA_VALIDATION_FAILED")
    nulls = sorted(k for k, v in arguments.items() if v is None)
    if nulls:  # absent optionals are omitted, never null (section 2.1)
        return None, failure(operation, request_id, "SCHEMA_VALIDATION_FAILED", details={"fields": nulls})
    try:
        return model.model_validate(arguments), None
    except ValidationError as exc:
        fields = sorted({".".join(str(p) for p in e["loc"]) for e in exc.errors()})
        return None, failure(operation, request_id, "SCHEMA_VALIDATION_FAILED", details={"fields": fields})


def _filters_exclude_this_kb(filters) -> bool:
    """filters bound what evidence is considered: a ceiling below this KB's
    classification, or lifecycle statuses without "active", leave none."""
    if filters is None:
        return False
    ceiling = filters.maxClassificationLevel
    level = card.service_card()["access"]["classificationLevel"]
    if ceiling is not None and (not isinstance(level, int) or level > ceiling):
        return True
    statuses = [str(s) for s in filters.lifecycleStatuses] if filters.lifecycleStatuses else None
    return statuses is not None and card.service_card()["lifecycle"]["status"] not in statuses


def _internal(operation: str, req, session_id: str | None, *, limit: int | None = None) -> A2KRequest:
    context = req.context
    return A2KRequest(
        operation=operation,
        query=req.query,
        context=RequestContext(purpose=str(context.purpose) if context and context.purpose else None),
        requirements=Requirements(
            maxEvidenceAgeSeconds=req.requirements.maxEvidenceAgeSeconds if req.requirements else None,
            reportConflicts=True,
        ),
        requestMetadata=RequestMetadata(requestId=req.requestId, sessionId=session_id),
        pagination=Pagination(limit=limit) if limit is not None else None,
    )


# --- shared response pieces ----------------------------------------------------


def _timestamp(value: str | None) -> str | None:
    return value if value and _TIMESTAMP.match(value) else None


def _vendor_api(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return any(host == h or host.endswith("." + h) for h in config.vendor_api_hosts)


def _citations(env: CitedResponseEnvelope) -> list[dict[str, Any]]:
    """0.1 citations with a permitted resolver: a URL (getDocument is not
    advertised) -- and not one of a vendor's own API, an internal record of
    the vendor rather than an origin document."""
    out = []
    for c in env.citations:
        url = c.sourceUrl if c.sourceUrl and _URI.match(c.sourceUrl) else None
        if url is None or _vendor_api(url):
            continue
        citation: dict[str, Any] = {
            "id": c.id,
            "documentId": c.documentId or url,
            "retrievedAt": _timestamp(c.retrievedAt) or _now(),
            "sourceUrl": url,
        }
        if c.title:
            citation["title"] = c.title
        if _timestamp(c.sourceLastUpdated):
            citation["sourceLastUpdated"] = c.sourceLastUpdated
        selector = c.selector
        if selector is not None and getattr(selector, "type", None) == "TextQuoteSelector" and selector.exact:
            citation["selector"] = {
                "type": "TextQuoteSelector",
                "exact": selector.exact,
                **({"prefix": selector.prefix} if selector.prefix else {}),
                **({"suffix": selector.suffix} if selector.suffix else {}),
            }
        out.append(citation)
    return out


def _success(model, operation: str, req, data: dict[str, Any]) -> dict[str, Any]:
    payload = {
        "a2kVersion": VERSION,
        "ok": True,
        "operation": operation,
        "requestId": req.requestId,
        "responseId": _response_id(),
        # Last, so every citation's retrievedAt is not after it (section 5.5 ordering).
        "generatedAt": _now(),
        "kbId": card.kb_id(),
        # Required on every success of a non-public KB (classification 1); S0: no audit record required.
        "accessDecision": {"decision": "allowed"},
        "audit": {"status": "not-required"},
        "data": data,
    }
    return model.model_validate(payload).model_dump(mode="json", exclude_none=True)


# --- ask -------------------------------------------------------------------------


def _refused(req, code: str, reason: str, *, retryable: bool = False) -> dict[str, Any]:
    data = {
        "outcome": _OUTCOME_OF.get(code, "refused"),
        "claims": [],
        "citations": [],
        "grounding": {"status": "unsupported", "method": _GROUNDING_METHOD},
        "refusal": {"code": code, "reason": reason, "retryable": retryable},
    }
    return _success(AskSuccess, "ask", req, data)


_MARKDOWN = set("*_`")


def _locate(text: str, answer: str) -> tuple[int, int] | None:
    """A claim's span in the answer (see _locate_exact). A claim the agent
    closed with a full stop where the answer's sentence goes on ("…no Reino
    Unido." for "…no Reino Unido e o seu histórico…") is found without it."""
    span = _locate_exact(text, answer)
    trimmed = text.strip().rstrip(".;:,")
    if span is None and trimmed and trimmed != text.strip():
        span = _locate_exact(trimmed, answer)
    return span


def _locate_exact(text: str, answer: str) -> tuple[int, int] | None:
    """Where a claim sits in the answer: an exact match; else the same words
    with any whitespace between them; else the same once markdown emphasis
    is ignored on both sides -- the agent writes "Q4 2022: $9.9 billion" for
    an answer line "- **Q4 2022:** $9.9 billion" (10-question check,
    2026-10-08: 30 of 204 claims lost that way). Offsets are code points of
    the answer as delivered (Python str indexes), as section 5.2 requires."""
    text = text.strip()
    if not text:
        return None
    start = answer.find(text)
    if start >= 0:
        return start, start + len(text)
    pattern = r"\s+".join(re.escape(token) for token in text.split())
    match = re.search(pattern, answer)
    if match:
        return match.start(), match.end()
    # Ignore markdown emphasis: search a copy without it, map positions back.
    kept = [i for i, ch in enumerate(answer) if ch not in _MARKDOWN]
    plain = "".join(answer[i] for i in kept)
    bare = "".join(ch for ch in text if ch not in _MARKDOWN).split()
    if not bare:
        return None
    match = re.search(r"\s+".join(re.escape(token) for token in bare), plain)
    if not match:
        return None
    return kept[match.start()], kept[match.end() - 1] + 1


def _claims(env: CitedResponseEnvelope, answer: str, usable: set[str]) -> list[dict[str, Any]]:
    """The agent's claims located in the answer, each citing only usable
    citations. A claim that is not literally in the answer is dropped -- its
    span cannot be stated -- and the text it covered stays uncovered."""
    claims = []
    for claim in env.claims:
        span = _locate(claim.text, answer)
        if span is None:
            continue
        cited = [cid for cid in claim.citationIds if cid in usable]
        if not cited:
            status = "INSUFFICIENT_EVIDENCE"
        elif claim.status in ("DISPUTED", "REFUTED"):
            status = claim.status
        else:
            status = "SUPPORTED"
        claims.append({
            "id": f"k{len(claims) + 1}",
            "text": claim.text.strip(),
            "status": status,
            "answerSpans": [{"start": span[0], "end": span[1]}],
            "citationIds": cited,
        })
    return claims


def _grounding_status(claims: list[dict[str, Any]]) -> str:
    supported = [c["status"] == "SUPPORTED" for c in claims]
    if supported and all(supported):
        return "supported"  # never strict-satisfied: the method is a self-report (section 5.6)
    return "partial" if any(supported) else "unsupported"


def ask_response(req: AskRequest, env: CitedResponseEnvelope) -> dict[str, Any]:
    if not env.ok:
        code = env.error.code if env.error else ""
        if code in _REFUSALS:
            return _refused(req, _REFUSALS[code], "The question could not be answered from the available sources.")
        return _from_internal_error("ask", req.requestId, env)
    answer = (env.answer or "").strip()
    if not answer or env.coverage == "none":
        return _refused(req, "INSUFFICIENT_EVIDENCE", "No information was found to answer this question.")
    if answer == config.vendor_fallback_message.strip():
        return failure("ask", req.requestId, "UPSTREAM_ERROR")
    answer = env.answer  # spans index the answer exactly as delivered
    citations = _citations(env)
    usable = {c["id"] for c in citations}
    claims = _claims(env, answer, usable)
    if not claims:
        # No claim of the agent's could be located (or there were none, as on a
        # cache hit): one claim spanning the whole answer, citing every usable citation.
        claims = [{
            "id": "k1",
            "text": answer,
            "status": "SUPPORTED" if citations else "INSUFFICIENT_EVIDENCE",
            "answerSpans": [{"start": 0, "end": len(answer)}],
            "citationIds": [c["id"] for c in citations],
        }]
    cited = {cid for c in claims for cid in c["citationIds"]}
    data: dict[str, Any] = {
        "outcome": "answered",
        "answer": answer,
        "claims": claims,
        # Every citation a claim references (section 4.2); the others are left out.
        "citations": [c for c in citations if c["id"] in cited],
        "grounding": {"status": _grounding_status(claims), "method": _GROUNDING_METHOD},
    }
    warnings = []
    if env.coverage == "partial":
        data["outcome"] = "partial"
        data["refusal"] = {"code": "INSUFFICIENT_EVIDENCE", "reason": "Part of the question could not be answered from the available sources.", "retryable": False}
        warnings += [f"Not answered: {m}" for m in env.missing if m.strip()] or ["Part of the question was not answered."]
    warnings += [f"The sources disagree: {c.thisPosition} / {c.otherPosition}" for c in env.conflicts]
    if warnings:
        data["warnings"] = warnings
    return _success(AskSuccess, "ask", req, data)


async def ask(arguments: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    req, error = _parse("ask", AskRequest, arguments)
    if error is not None:
        return error
    features = [str(f) for f in (req.requirements.features or [])] if req.requirements else []
    if "strict-grounding" in features:
        # The answer's grounding is the agent's self-report, which can never be strict-satisfied.
        return failure("ask", req.requestId, "REQUIRED_FEATURE_UNSUPPORTED", details={"unsupportedFeatures": ["strict-grounding"]})
    if req.requirements and req.requirements.maxEvidenceAgeSeconds is not None:
        return _refused(req, "EVIDENCE_TOO_OLD", "No evidence here carries a verification time from its origin, so none can meet a maximum evidence age.")
    context = req.context
    if context and context.asOf and not card.point_in_time_supported():
        return _refused(req, "POINT_IN_TIME_UNSUPPORTED", "Answers as of a past instant are not supported.")
    if not card.purpose_allowed(str(context.purpose) if context and context.purpose else "answering"):
        return _refused(req, "PURPOSE_NOT_ALLOWED", "This purpose is not allowed for this knowledge base.")
    if _filters_exclude_this_kb(req.filters):
        return _refused(req, "INSUFFICIENT_EVIDENCE", "No evidence is permitted under the requested filters.")
    env = await engine.ask(_internal("ask", req, session_id))
    return ask_response(req, env)


# --- search ----------------------------------------------------------------------


def search_response(req: SearchRequest, env: CitedResponseEnvelope) -> dict[str, Any]:
    if not env.ok:
        code = env.error.code if env.error else ""
        if code in _REFUSALS:  # no outcome on search: an empty, successful result
            return _success(SearchSuccess, "search", req, {"passages": [], "citations": [], "pageInfo": {"nextCursor": None}})
        return _from_internal_error("search", req.requestId, env)
    citations = {c["id"]: c for c in _citations(env)}
    passages, used = [], []
    for p in env.passages:
        ids = [cid for cid in p.citationIds if cid in citations]
        if not ids or not p.text:
            continue  # a passage is located by at least one usable citation
        passage: dict[str, Any] = {"id": p.id, "text": p.text, "citationIds": ids}
        if p.score is not None and 0 <= p.score <= 1:
            passage["score"] = p.score
        passages.append(passage)
        used.extend(cid for cid in ids if cid not in used)
    data = {"passages": passages, "citations": [citations[cid] for cid in used], "pageInfo": {"nextCursor": None}}
    return _success(SearchSuccess, "search", req, data)


async def search(arguments: dict[str, Any], *, session_id: str | None = None) -> dict[str, Any]:
    req, error = _parse("search", SearchRequest, arguments)
    if error is not None:
        return error
    context = req.context
    if context and context.asOf and not card.point_in_time_supported():
        return failure("search", req.requestId, "INVALID_REQUEST")
    if not card.purpose_allowed(str(context.purpose) if context and context.purpose else "answering"):
        return failure("search", req.requestId, "ACCESS_DENIED")
    pagination = req.pagination
    if pagination and pagination.cursor:
        # No continuation support: every result is one page (section 7 rule 4).
        return failure("search", req.requestId, "PAGINATION_CURSOR_INVALID")
    if _filters_exclude_this_kb(req.filters):
        return _success(SearchSuccess, "search", req, {"passages": [], "citations": [], "pageInfo": {"nextCursor": None}})
    if req.requirements and req.requirements.maxEvidenceAgeSeconds is not None:
        # search returns only qualifying evidence (section 5.5): none qualifies here.
        return _success(SearchSuccess, "search", req, {
            "passages": [], "citations": [], "pageInfo": {"nextCursor": None},
            "warnings": ["No evidence here carries a verification time from its origin, so none meets the maximum evidence age."],
        })
    limit = pagination.limit if pagination and pagination.limit else 10
    env = await engine.search(_internal("search", req, session_id, limit=limit))
    return search_response(req, env)
