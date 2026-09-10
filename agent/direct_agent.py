"""Two-phase "discover and decide" agent logic for the mcp_to_agent_to_mcp
branch -- called by entrypoint_a2k.py, which a2k-box's gateway/engine.py
invokes (over HTTPS, Cognito Bearer token) instead of running its own
deterministic Cala/Sayari adapters.

Phase 1 -- vendor decision: a cheap, tool-less structured-output call over
the catalogue a2k-box already loaded and passed in (no MCP connection paid
for a vendor that turns out not to be needed). Skipped entirely when the
caller already specified `sources` explicitly.

Phase 2 -- per chosen vendor: open a direct connection to that vendor's own
MCP (vendor_mcp_client.py), hand the model whatever tools that vendor's
`tools/list` returns (live discovery -- no hardcoded search_entities-then-
get_entity_summary sequence the way a2k/adapters/sayari_mcp.py has), and let
it decide what to call and how, then produce a structured result via
Strands' `structured_output_model`.

Citations are referenced by *index* into each response's own `citations[]`
list (`citationIndexes`), not by an LLM-invented string ID -- a2k-box's
gateway/engine.py assigns the real sequential `citation-N`/`claim-N` IDs
when assembling the final envelope, mirroring how gateway/synthesis.py
already does ID assignment on the Gateway-mediated path today. Everything
this module returns is honestly labeled as *self-reported* by the model
(`groundedRatio`, conflict detection) -- this is a materially weaker
guarantee than gateway/synthesis.py's exact, deterministic one; see the
plan this branch was built from for why that trade was made anyway.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, Field
from strands import Agent
from strands.models import BedrockModel
from strands.vended_plugins.skills import AgentSkills

import observability
import vendor_mcp_client

# --- Structured-output shapes ------------------------------------------------


class VendorDecision(BaseModel):
    sourceIds: list[str] = Field(description="Active vendor sourceId(s) this query should go to.")
    reasoning: str = Field(description="One sentence on why these vendor(s) were chosen.")


class CitationOut(BaseModel):
    title: str | None = None
    sourceUrl: str | None = None
    documentId: str | None = None


class ClaimOut(BaseModel):
    text: str
    type: str | None = None
    status: Literal["SUPPORTED", "DISPUTED", "INSUFFICIENT_EVIDENCE"] = "SUPPORTED"
    citationIndexes: list[int] = Field(
        default_factory=list, description="Indexes into this response's own citations[] list."
    )


class PassageOut(BaseModel):
    text: str
    citationIndexes: list[int] = Field(default_factory=list)


class AskContent(BaseModel):
    answer: str
    claims: list[ClaimOut] = Field(default_factory=list)
    citations: list[CitationOut] = Field(default_factory=list)
    groundedRatio: float = Field(
        description="Self-reported estimate (0.0-1.0) of how much of `answer` is directly "
        "supported by the cited tool results. Not a verified/exact measurement -- see module docstring."
    )


class SearchContent(BaseModel):
    passages: list[PassageOut] = Field(default_factory=list)
    citations: list[CitationOut] = Field(default_factory=list)


class ConflictOut(BaseModel):
    thisClaimIndex: int = Field(description="Index into the merged claims[] list.")
    otherClaimIndex: int = Field(description="Index into the merged claims[] list.")
    nature: str
    assessment: str
    rationale: str


class ConflictCheck(BaseModel):
    conflicts: list[ConflictOut] = Field(default_factory=list)


# --- Phase 1: vendor decision -------------------------------------------------

_VENDOR_DECISION_PROMPT = """You are choosing which company-intelligence \
vendor(s) a query should be sent to. You are given the current vendor \
catalogue below (already fetched -- do not assume this list, treat it as \
authoritative for this request):

{catalogue}

Query: {query}

Rules:
1. Only vendors with status "active" are eligible.
2. Match the query against each active vendor's domains/topics/scope --
not against assumptions about vendor names.
3. Prefer returning exactly one sourceId. Each active vendor's scope is
meant to be distinct (e.g. financial/legal/regulatory filings vs.
ownership/risk graph) -- a query that's a genuine match for one vendor's
scope is almost never *also* a genuine match for another's just because
both vendors happen to cover companies in general. Pick the single
best-matching vendor whenever one is a reasonable fit.
4. Only return more than one sourceId when the query itself genuinely
spans more than one vendor's distinct scope (e.g. it explicitly asks for
both financial filings AND ownership structure) -- not merely because
several vendors could theoretically produce tangentially related
results. This should be rare; when in doubt, pick one.
5. If no active vendor matches at all, return an empty sourceIds list.
"""


def _format_catalogue(catalogue: list[dict]) -> str:
    blocks = []
    for v in catalogue:
        blocks.append(
            f"- sourceId={v.get('sourceId')!r} status={v.get('status')!r} "
            f"queryType={v.get('queryType')!r}\n"
            f"  domains={v.get('domains')!r} topics={v.get('topics')!r}\n"
            f"  scope: {v.get('scope')}"
        )
    return "\n".join(blocks) if blocks else "(no vendors in catalogue)"


def _decide_vendors_sync(query: str, catalogue: list[dict], model_id: str, region: str) -> list[str]:
    model = BedrockModel(model_id=model_id, region_name=region)
    agent = Agent(model=model, tools=[], callback_handler=None, structured_output_model=VendorDecision)
    prompt = _VENDOR_DECISION_PROMPT.format(catalogue=_format_catalogue(catalogue), query=query)
    result = agent(prompt)
    decision: VendorDecision = result.structured_output
    return decision.sourceIds


# --- Phase 2: per-vendor discovery + call -------------------------------------

_ASK_SYSTEM_PROMPT = """You are a company-intelligence assistant with live \
access to {vendor_name}'s own tools (listed below -- discover what each one \
does from its own description, there is no fixed sequence to follow). \
Use whatever tool(s) are appropriate to answer the caller's question, then \
produce your final answer in the required structured format: `answer` (the \
synthesized response), `claims` (one per distinct factual assertion, each \
citing the citations[] entries that support it by index), `citations` (one \
per distinct source you actually used), and `groundedRatio` (your own \
honest estimate of how much of `answer` is directly backed by what the \
tools returned, not general knowledge). Never invent facts the tools didn't \
return -- if you don't have enough to answer, say so in `answer` and set a \
low `groundedRatio`. When reporting identifiers (registration/company \
numbers, LEIs, case references, etc.) or a relationship's direction (who \
owns/controls/is-a-subsidiary-of whom), copy them exactly as the tool \
result stated them -- re-read the specific field before writing it down \
rather than recalling it from memory, since transposing a digit or \
reversing which party is the parent and which is the subsidiary is a \
factual error even when everything else in the answer is right.

Query: {query}
"""

_SEARCH_SYSTEM_PROMPT = """You are retrieving relevant raw passages from \
{vendor_name}'s own tools (listed below -- discover what each one does from \
its own description, there is no fixed sequence to follow) for the caller's \
query. Do not synthesize an answer -- return the relevant raw material you \
found as `passages` (one per distinct fact/excerpt) and `citations` (one \
per distinct source), citing each passage's supporting citations[] entries \
by index.

Query: {query}
"""


# TEMPORARY, Cala-only testing (2026-09-10) -- the vendor's own Agent Skill
# (https://docs.cala.ai/integrations/agent-skill), loaded live via Strands'
# AgentSkills plugin (strands.vended_plugins.skills -- Skill.from_url() fetches
# and parses the raw SKILL.md at Agent-construction time, nothing copied into
# this repo). Sayari has no equivalent yet, and this isn't wired into any
# config/env-var gate on purpose -- it's meant to be easy to find and revert
# (see _run_vendor_agent_sync's `plugins=` line below), not a permanent
# per-vendor mechanism.
_CALA_SKILL_URL = "https://raw.githubusercontent.com/cala-ai/cala-skill/main/SKILL.md"


@dataclass
class VendorCallResult:
    content: AskContent | SearchContent
    invocation_count: int
    error_count: int
    usage: dict  # {"inputTokens": int, "outputTokens": int, "totalTokens": int}
    tool_calls: list[dict]  # [{"vendor", "toolName", "input", "output", "status", "errorType"}, ...]


def _run_vendor_agent_sync(
    *,
    operation: str,
    query: str,
    source_id: str,
    model_id: str,
    region: str,
    session_id: str | None = None,
    internal_client: str | None = None,
) -> VendorCallResult:
    """Returns (structured_output, invocation_count, error_count) -- the
    latter two come from the ToolCallLogger hook below, so handle() can sum
    them across every vendor queried (there can be more than one on fan-out)
    into a single observability.emit_query_metrics() call for the whole
    request, same idea as core.py's ask() does for the Gateway-mediated path.

    A fresh ToolCallLogger per call, not shared across vendors: `chosen_vendor`
    inside its log lines stays None here (Sayari's/Cala's own tool names,
    unlike core.py's a2k.ask, carry no `sources` argument to read it from --
    the tool name itself is vendor-specific enough to tell them apart in the
    logs), but the invocation/error tallying and tool-call log lines are
    still useful as-is.
    """
    mcp_client, tools = vendor_mcp_client.connect_vendor(source_id)
    try:
        # temperature=0: ground_truth_v4.csv's live run (2026-09-08) caught a
        # real hallucination here -- same Sayari entity match (identical
        # graph node URL) synthesized correctly in one run and, in another
        # run of the exact same query, came back with a fabricated
        # registration number and an inverted ownership direction (claimed
        # the parent owned the subsidiary, backwards). That's a synthesis
        # error, not a retrieval/routing one -- lower temperature biases
        # this step toward literal reproduction of what the tools returned
        # instead of a paraphrase that can quietly transpose a number or
        # flip a relationship. Phase 1's vendor decision and the conflict
        # checker are left at the default -- this hallucination was
        # specifically in reading tool results back out, not in either of
        # those.
        model = BedrockModel(model_id=model_id, region_name=region, temperature=0)
        if operation == "ask":
            system_prompt = _ASK_SYSTEM_PROMPT.format(vendor_name=source_id, query=query)
            output_model = AskContent
        else:
            system_prompt = _SEARCH_SYSTEM_PROMPT.format(vendor_name=source_id, query=query)
            output_model = SearchContent
        tool_logger = observability.ToolCallLogger(
            session_id=session_id, internal_client=internal_client, verbose=True
        )
        # TEMPORARY -- see _CALA_SKILL_URL above. Revert by deleting this
        # `if` and passing plugins=None (or dropping the kwarg) unconditionally.
        plugins = [AgentSkills(skills=_CALA_SKILL_URL)] if source_id == "cala" else None
        agent = Agent(
            model=model,
            tools=tools,
            system_prompt=system_prompt,
            callback_handler=None,
            structured_output_model=output_model,
            hooks=[tool_logger],
            plugins=plugins,
        )
        result = agent(query)
        tool_calls = [
            {
                "vendor": source_id,
                "toolName": c["tool_name"],
                "input": c["input"],
                "output": c["output"],
                "status": c["status"],
                "errorType": c["error_type"],
            }
            for c in tool_logger.calls
        ]
        return VendorCallResult(
            content=result.structured_output,
            invocation_count=tool_logger.invocation_count,
            error_count=tool_logger.error_count,
            usage=dict(result.metrics.accumulated_usage),
            tool_calls=tool_calls,
        )
    finally:
        vendor_mcp_client.disconnect(mcp_client)


# --- Merging multi-vendor results ---------------------------------------------


def _merge_ask_contents(
    contents: list[AskContent], source_ids: list[str], *, model_id: str, region: str
) -> dict:
    """Concatenates claims/citations across vendors (re-indexing
    citationIndexes to the merged citations[] list, and tagging each claim
    with the vendor it came from), joins answers, averages groundedRatio, and
    runs one extra self-reported conflict-check pass if more than one vendor
    actually returned claims -- see module docstring on why this whole thing
    is self-reported, not a verified comparison the way gateway/conflict.py's
    deterministic field-by-field check is."""
    merged_citations: list[dict] = []
    merged_claims: list[dict] = []
    claim_vendor_ids: list[str] = []
    answers: list[str] = []
    ratios: list[float] = []

    for source_id, content in zip(source_ids, contents):
        offset = len(merged_citations)
        merged_citations.extend(c.model_dump() for c in content.citations)
        for claim in content.claims:
            claim_dict = claim.model_dump()
            claim_dict["citationIndexes"] = [i + offset for i in claim.citationIndexes]
            merged_claims.append(claim_dict)
            claim_vendor_ids.append(source_id)
        if content.answer:
            answers.append(content.answer)
        ratios.append(content.groundedRatio)

    conflicts: list[dict] = []
    if len(contents) > 1 and merged_claims:
        conflicts = _check_conflicts_sync(merged_claims, claim_vendor_ids, model_id=model_id, region=region)

    return {
        "answer": "\n\n".join(answers) if answers else None,
        "claims": merged_claims,
        "citations": merged_citations,
        "groundedRatio": sum(ratios) / len(ratios) if ratios else 0.0,
        "conflicts": conflicts,
    }


def _merge_search_contents(contents: list[SearchContent]) -> dict:
    merged_citations: list[dict] = []
    merged_passages: list[dict] = []
    for content in contents:
        offset = len(merged_citations)
        merged_citations.extend(c.model_dump() for c in content.citations)
        for passage in content.passages:
            passage_dict = passage.model_dump()
            passage_dict["citationIndexes"] = [i + offset for i in passage.citationIndexes]
            merged_passages.append(passage_dict)
    return {"passages": merged_passages, "citations": merged_citations}


_CONFLICT_CHECK_PROMPT = """These are claims gathered from different vendors \
about the same query, listed by index. Identify any pairs that genuinely \
disagree on a fact (not just cover different aspects, and not just two \
vendors independently reporting the same fact) -- report each as a \
conflict between the two claim indexes involved, with your assessment and \
rationale. If nothing genuinely conflicts, return an empty list.

Claims (index: vendor -- text):
{claims_text}
"""


def _check_conflicts_sync(
    claims: list[dict], claim_vendor_ids: list[str], *, model_id: str, region: str
) -> list[dict]:
    claims_text = "\n".join(f"{i}: [{vendor}] {c['text']}" for i, (vendor, c) in enumerate(zip(claim_vendor_ids, claims)))
    model = BedrockModel(model_id=model_id, region_name=region)
    agent = Agent(model=model, tools=[], callback_handler=None, structured_output_model=ConflictCheck)
    result = agent(_CONFLICT_CHECK_PROMPT.format(claims_text=claims_text))
    check: ConflictCheck = result.structured_output
    return [c.model_dump() for c in check.conflicts]


# --- Public entry point --------------------------------------------------------


async def handle(
    *,
    operation: str,
    query: str,
    sources: list[str] | None,
    catalogue: list[dict],
    model_id: str,
    region: str = "eu-west-1",
    session_id: str | None = None,
    internal_client: str | None = None,
) -> dict:
    """Top-level entry point -- entrypoint_a2k.py calls this directly.
    Returns a plain JSON-serializable dict matching AskContent's or
    SearchContent's shape (with `citationIndexes` re-indexed and merged
    across vendors, and `conflicts` added for `ask`), plus two audit/
    instrumentation extras summed across every vendor phase 2 actually
    queried: `usage` (`inputTokens`/`outputTokens`/`totalTokens`, from
    Strands' `AgentResult.metrics.accumulated_usage`) and `toolCalls`
    (one entry per tool call actually made, each tagged with which vendor
    it went to -- see observability.ToolCallLogger.calls). gateway/engine.py
    maps both into the response envelope's `usage`/`toolCalls` fields.

    Runs Strands' (blocking) calls via asyncio.to_thread so that, when
    `sources` spans multiple vendors, each vendor's phase-2 discovery+call
    genuinely runs in parallel rather than one after another.

    `session_id`/`internal_client` are purely for observability.py (see
    entrypoint_a2k.py, which threads a2k-box's own requestId/label through
    here) -- one emit_query_metrics() call covers the whole request,
    summing invocation/error counts across every vendor phase 2 actually
    queried (there can be more than one on fan-out), same grain as
    core.py's ask() emits one per Gateway-mediated call.
    """
    query_start = time.monotonic()
    total_invocation_count = 0
    total_error_count = 0
    query_errored = False
    try:
        active_ids = {v["sourceId"] for v in catalogue if v.get("status") == "active"}
        source_ids = [s for s in sources if s in active_ids] if sources else None
        if source_ids is None:
            source_ids = await asyncio.to_thread(_decide_vendors_sync, query, catalogue, model_id, region)
        source_ids = [s for s in source_ids if s in active_ids]

        if not source_ids:
            empty_usage = {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}
            if operation == "ask":
                return {
                    "answer": None,
                    "claims": [],
                    "citations": [],
                    "groundedRatio": 0.0,
                    "conflicts": [],
                    "usage": empty_usage,
                    "toolCalls": [],
                }
            return {"passages": [], "citations": [], "usage": empty_usage, "toolCalls": []}

        results: list[VendorCallResult] = await asyncio.gather(
            *(
                asyncio.to_thread(
                    _run_vendor_agent_sync,
                    operation=operation,
                    query=query,
                    source_id=sid,
                    model_id=model_id,
                    region=region,
                    session_id=session_id,
                    internal_client=internal_client,
                )
                for sid in source_ids
            )
        )
        contents = []
        total_input_tokens = 0
        total_output_tokens = 0
        total_all_tokens = 0
        all_tool_calls: list[dict] = []
        for r in results:
            contents.append(r.content)
            total_invocation_count += r.invocation_count
            total_error_count += r.error_count
            total_input_tokens += r.usage.get("inputTokens") or 0
            total_output_tokens += r.usage.get("outputTokens") or 0
            total_all_tokens += r.usage.get("totalTokens") or 0
            all_tool_calls.extend(r.tool_calls)

        usage = {"inputTokens": total_input_tokens, "outputTokens": total_output_tokens, "totalTokens": total_all_tokens}
        if operation == "ask":
            merged = _merge_ask_contents(contents, source_ids, model_id=model_id, region=region)
        else:
            merged = _merge_search_contents(contents)
        merged["usage"] = usage
        merged["toolCalls"] = all_tool_calls
        return merged
    except Exception:
        query_errored = True
        raise
    finally:
        observability.emit_query_metrics(
            internal_client=internal_client,
            latency_ms=(time.monotonic() - query_start) * 1000,
            invocation_count=total_invocation_count,
            error_count=total_error_count,
            query_errored=query_errored,
        )
