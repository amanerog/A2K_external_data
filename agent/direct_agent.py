"""Two-phase "discover and decide" agent logic for the mcp_to_agent_to_mcp
branch -- called by entrypoint_a2k.py, which a2k-box's gateway/engine.py
invokes (over HTTPS, Cognito Bearer token) instead of running its own
deterministic Cala/Sayari adapters.

Phase 1 -- vendor decision: a cheap, tool-less structured-output call over
the catalogue a2k-box already loaded and passed in (no MCP connection paid
for a vendor that turns out not to be needed). Skipped entirely when the
caller already specified sources explicitly.

Phase 2 -- per chosen vendor: open a direct connection to that vendor's own
MCP (vendor_mcp_client.py), hand the model whatever tools that vendor's
tools/list returns (live discovery -- no hardcoded search_entities-then-
get_entity_summary sequence the way a2k/adapters/sayari_mcp.py has), and let
it decide what to call and how, then produce a structured result via
Strands' structured_output_model.

Citations are referenced by index into each response's own citations[]
list (citationIndexes), not by an LLM-invented string ID -- a2k-box's
gateway/engine.py assigns the real sequential citation-N/claim-N IDs
when assembling the final envelope, mirroring how gateway/synthesis.py
already does ID assignment on the Gateway-mediated path today. Everything
this module returns is honestly labeled as self-reported by the model
(groundedRatio, conflict detection) -- this is a materially weaker
guarantee than gateway/synthesis.py's exact, deterministic one; see the
plan this branch was built from for why that trade was made anyway.

--------------------------------------------------------------------------
v5 (2026-09-28) -- Sayari's own prompting guidance incorporated for the first time

Until now, VENDOR_REINFORCEMENT["sayari"] was built entirely from our own audit and from
Sayari's self-assessment data, because no vendor-authored guidance existed to draw on (see v2
changelog item 7). That changed: Sayari published its own prompt library in September 2026
(Pruebas v3/Sayari_MCP Prompt Library_2026.pdf, "for teams running Sayari MCP"), organized as
ten domains plus a risk-factor-taxonomy appendix. Most of the Sayari block below is now built
from that document; the two points that were already there from our own audit (recognizing
out-of-scope questions quickly, using discovery tools before guessing an identifier format)
are kept and cross-referenced against it, since nothing in it contradicts them.

IMPORTANT DIFFERENCE FROM CALA/LINKUP: this is not a live-loaded skill. Cala's and Linkup's
guidance is fetched from their own repositories at call time (VENDOR_SKILLS below), so a
vendor update reaches this agent automatically. Sayari has no entry in VENDOR_SKILLS -- this
is a static document we read once and incorporated by hand into VENDOR_REINFORCEMENT["sayari"].
If Sayari revises this guide later, nothing here updates on its own; it needs the same manual
re-read this file itself gets when new evidence shows up.

What was added, by source in the document:
  - Multiple entity profiles per company (a registry entity, a trade-only shipper, a branch, an
    unconfirmed candidate match) can all exist for the same real company; picking one "best
    match" silently discards whatever shipment history or risk flags attach only to the others.
  - A nil/empty result is ambiguous by construction: the record may not exist, the jurisdiction
    may not publish it, or Sayari's own coverage there may be partial. The document is explicit
    that these look identical in an answer, so silence needs to be interpreted, not read as a
    clean negative.
  - Ownership tracing needs both directions, not just upward: the beneficial owner (a natural
    person) and the global ultimate owner (often another entity) are two different things worth
    naming separately, and risk factors like forced-labor or export-control links attach to what
    an entity owns or controls downstream -- an upward-only trace (the only direction most
    people think to ask for) never sees them.
  - A named person recorded as a shareholder or director on one filing is not the same as that
    person's own record: the filing line typically carries only one nationality and address,
    while the person's full recorded footprint (other nationalities, other roles, other
    companies) sits on their own profile and can bring different sanctions or export-control
    programs into scope entirely.
  - Several small but concrete precision points that all serve the same purpose -- keeping a
    finding traceable to exactly what Sayari recorded, not to an inference layered on top of it
    by the agent: ask for a risk finding under its own recorded name rather than letting the
    model estimate one from general knowledge (mapping a commodity code to a control list from
    memory, judging whether a stake crosses a threshold); report a negative as the check
    performed ("no match on the list actually searched"), not as a "cleared" verdict, since the
    two read identically in a file months later; keep a relationship's recorded direction
    (owns/is owned by, controls/is controlled by) rather than collapsing it into one vaguer
    description; and don't let a jurisdiction-level index (a country's AML or corruption
    ranking) aggregate into what reads like a company-specific finding.
  - Sayari's own explicit reliability limit: it answers one entity/commodity/period/chain well,
    but asked to enumerate an entire network or trade lane, it can silently return a partial
    slice while looking exactly like a complete answer. This is the same problem CONSOLIDATION's
    new rule 3 below now names generically (see that rule for why it was promoted out of a
    single vendor's block) -- Sayari's own block now points back to it and adds this vendor's
    concrete mechanics for avoiding it.

Also reviewed, and deliberately left out: a second PDF in the same folder
(Sayari_Santander Test Entities Review_September 2026.pdf) is not usage guidance -- it is
Sayari's rebuttal of the POC2 benchmark's grading, showing that 6 of the 12 questions it was
graded against used fabricated test companies (invalid checksums, placeholder example.test
URLs and references) rather than real registry data. That is a real and well-evidenced concern,
but it is about the benchmark's own ground truth, not about how this agent should call Sayari's
tools, so nothing from it belongs in this file.

--------------------------------------------------------------------------
v4 (2026-09-28) -- walked back two of v3's relaxations; restored a changelog entry v3 dropped

Reviewed v3 right after writing it and found that two of its six changes reopened exactly the
failure modes their originating rules existed to prevent, without any new evidence that the
stricter version had actually been causing a problem:

  1. v3's item 2 below let a multi-fact question be answered with one call whenever the live tool
     description or vendor skill "says those facts are naturally retrieved together" -- but that
     phrasing hands the decision to the model's own sense of what belongs together, which is the
     exact judgment call that failed in the audit: Linkup folded a CET1-ratio-and-NPL-ratio
     question into one combined query, twice, on the same question (#20 in POC2.1) that a
     single-fact-per-call approach (Cala's, on the identical question) answered fully -- and
     Linkup's version landed as Partial, not Covered. Trusting the model's impression that two
     facts are "naturally together" reopens that same failure with nothing new to justify it.
     Fixed in BUILD EACH TOOL CALL FROM ONLY WHAT THAT TOOL NEEDS (ASK_SYSTEM_PROMPT) and the
     equivalent paragraph v3 added to SEARCH_SYSTEM_PROMPT: the exception is now tied to something
     a live tool description can actually state on its own -- a single call whose documented
     output already returns more than one of the asked-for fields together -- not to the model's
     own impression that two facts feel related.
  2. v3's item 3 below loosened CONSOLIDATION's fidelity rules with an "answer-bearing" qualifier,
     letting the model decide a data point was "incidental" or "non-answer-bearing" and leave it
     out of the final answer. But the only failure mode the evidence actually documents on this
     point is under-inclusion, not over-inclusion -- v2 changelog item 5 below: six of Cala's
     POC2.1 questions failed because the final answer repeated the generic content of the first
     tool call instead of the specific figure the question asked for. There is no comparable case
     of an answer failing because it reproduced too much. Fixed: the exception is now narrowed
     specifically to a raw search-result candidate set the caller did not ask to see (many hits
     from a search tool, most not actually about the question) -- rule 1's wording no longer lets
     the model drop a data point on its own sense that the point is "incidental."
  3. v3 (see its own entry just below) accidentally dropped v2's changelog item 11, which
     documented why and when VENDOR_REINFORCEMENT["linkup"] was expanded from Linkup's own
     September 2026 self-assessment. The content of VENDOR_REINFORCEMENT["linkup"] itself was
     never touched by v3 and stayed correct throughout -- only the record of where it came from
     went missing from this docstring. Restored below, back in its original place in the v2
     changelog list.

Everything else v3 introduced -- explicit VENDOR NOTES precedence (item 1 below), period-aware
handling of time-stamped values (item 4), the semantic rather than rigidly literal answer-opening
rule (item 5), and the four-anchor groundedRatio calibration (item 6) -- is unchanged and still
applies as v3 describes it below.

--------------------------------------------------------------------------
v3 (2026-09-28) -- targeted prompt refinements for Sonnet 4.5

This revision deliberately keeps v2's architecture and evidence-derived guidance intact. It only
tightens six places where an absolute rule could cause over-compliance or ambiguous behaviour:

  1. Vendor-specific precedence is now explicit: a VENDOR NOTES rule that explicitly states an
     exception to a generic rule takes precedence for that vendor/tool. This makes deliberate
     exceptions (notably Linkup query construction) unambiguous.
  2. Multi-fact questions now default to one targeted retrieval unit per distinct fact, but may
     keep facts together when the live tool description or vendor skill says they are naturally
     retrieved together. This preserves the POC lesson without turning it into a universal
     one-call-per-fact law. [Revised in v4 above -- the exception is now tied to what a tool
     description documents, not to the model's own sense of what belongs together.]
  3. CONSOLIDATION now preserves every distinct answer-bearing fact relevant to the question,
     rather than encouraging verbatim carry-over of secondary or duplicate material. Lists are
     still exhaustive when the list itself is the answer, but raw search-result sets need not be
     reproduced in full unless completeness is semantically required. [Revised in v4 above -- the
     exception is now narrowed specifically to raw search-result candidate sets.]
  4. Time-stamped values are no longer treated as conflicts merely because they differ across
     dates. Values are DISPUTED only when they purport to describe the same fact for the same
     effective period and disagree. For time series, use the period the question asks for; for
     current/latest asks, use the newest returned value.
  5. The answer-opening requirement is now semantic rather than stylistically rigid: the opening
     sentence must make the subject and requested scope independently identifiable, without
     forcing an awkward near-restatement of complex questions.
  6. groundedRatio remains a float for contract compatibility but is now calibrated to four
     allowed anchor values (1.0 / 0.75 / 0.5 / 0.0) with explicit meanings, reducing false
     precision and improving reproducibility.

--------------------------------------------------------------------------
v2 (2026-09-22) -- what changed and why

This revision was written after cross-referencing three things: POC2
(Informe_POC2.html), POC2.1 (Informe_POC_2_1.html, a rerun on an edited
33-question set with a fixed AskContent consolidation step), and a direct
read of vendor_audit_v4_1_full_trace.json -- the actual tool-call inputs
our agent sent, not just the graded outcomes. Also folded in is direct
written feedback from Linkup on our POC2 results.

IMPORTANT CONSTRAINT, confirmed after the first draft of this file: Cala's
and Linkup's skills are loaded straight from each vendor's own repository
(see VENDOR_SKILLS below) specifically so that when a vendor updates its
skill, we pick that update up automatically. We do not maintain a fork of
either skill and are not going to -- an earlier draft of this file tried to
make the prompts fully vendor-agnostic and push every vendor-specific fix
into a locally-authored replacement skill; that's off the table. Every fix
below that needed vendor-specific knowledge lives in this file instead, as
a short, explicitly-labeled VENDOR_REINFORCEMENT block per vendor -- not a
rewrite of what the vendor's own skill already says, but a pointed repeat
of the small number of things our own audit shows are being read but not
applied. This is the same mechanism the original v1 of this file already
used for Linkup (LINKUP_TOOL_PRIORITY_GUIDANCE), generalized and, crucially,
fixed to actually reach both operations -- see point 1 below.

Findings that changed something in this file:

  1. LINKUP_TOOL_PRIORITY_GUIDANCE (vendor-specific: depth, outputType) was
     only ever injected into SEARCH_SYSTEM_PROMPT, for operation="search".
     It was never injected into ASK_SYSTEM_PROMPT, i.e. never seen on the
     operation="ask" path that AskContent runs and that essentially every
     audited question went through. That routing gap is the most likely
     single reason outputType=sourcedAnswer was sent on 0 of the ~300
     Linkup calls audited across POC2 and POC2.1, and depth=deep was used
     on only a handful of the 30+ questions where Linkup's own ground
     truth names it as the right depth. Fix: VENDOR_REINFORCEMENT below is
     now assembled by _build_system_prompt() and appended to whichever of
     ASK_SYSTEM_PROMPT / SEARCH_SYSTEM_PROMPT is in play, for every vendor
     that has an entry -- there is now exactly one place a vendor's
     reinforcement text is written, and one code path that inserts it
     regardless of operation.
  2. The raw trace showed the same two query-construction habits recurring
     across more than one vendor, with no rule in this file addressing
     either: building a search query around a figure the agent had itself
     generated but not yet seen returned by any tool (e.g. Cala's question
     #36 on EU car registrations, Linkup's #30 on Brazilian aggregate
     credit, both in POC2.1 -- the query embeds a number that only shows up
     because the model put it there), and "retrying" by only lightly
     rewording the same free-text query rather than changing what the call
     actually does (Linkup #26, #29, #55 in POC2.1: near-duplicate queries
     a few words apart, sometimes eight calls deep, all at the same
     depth). Both now have their own explicit, vendor-agnostic rule below,
     since neither is specific to one vendor's tools.
  3. "Ask the caller for an exact identifier instead of searching with the
     name already given" showed up identically on Cala and Linkup, on the
     same question (the Brazilian CNPJ lookup, #32 in POC2.1) -- both
     agents went straight to AskContent without ever calling a vendor tool.
     CONSOLIDATION's existing "if you can search, search" rule is now
     explicit that a bare name is normally enough to attempt a first call.
  4. Two failure modes had no corresponding rule at all: returning real,
     well-formed data about the wrong entity (Linkup #41, #52, #73 in
     POC2.1 -- a similarly-named or related-but-different company), and
     silently keeping a stale value when the same tool call's own output
     also contained a more recent, contradicting one (Linkup #26). Both
     are now explicit CONSOLIDATION items, and both are vendor-agnostic --
     they showed up on Linkup's data but nothing about them is Linkup-
     specific, so they live in the shared rules, not in a reinforcement
     block.
  5. The self-check rule existed but evidently wasn't specific enough: in
     POC2.1, six of Cala's in-scope questions ended up "not covered"
     because the final answer repeated the generic content of the first
     tool call instead of the specific figure, source or named example the
     question asked for. The self-check rule below now says explicitly
     what "checking against the raw output" has to verify.
  6. Two genuinely vendor-specific gaps remain, and now live in
     VENDOR_REINFORCEMENT: Linkup's own skill already explains outputType
     and depth at length, but our audit shows outputType=sourcedAnswer was
     never sent and depth=deep was used far less often than the vendor's
     own ground truth recommends -- restated below with the evidence, not
     rewritten. Cala's own skill already explains knowledge_query's dot-
     notation query language, but our audit shows it was only ever called
     with the same free text already sent to knowledge_search, never with
     that structured syntax -- also restated below, not rewritten.
  7. Sayari has no vendor-provided skill loaded at all (see VENDOR_SKILLS
     below -- there is no entry for it), so unlike Cala's and Linkup's
     blocks, VENDOR_REINFORCEMENT["sayari"] is not a repeat of external
     guidance -- it's original guidance drawn directly from our own audit
     and from Sayari's own self-assessment data, because no vendor skill
     exists to draw on. Three things stood out: Sayari's self-assessment is
     unusually reliable (every question it marks out of scope also
     independently graded as not covered), yet the agent still spent 2-3
     search_entities attempts chasing an organization name on questions
     clearly outside Sayari's entity/risk-graph scope (a consultancy's
     report, an EU-wide capital ratio) before giving up; Sayari's own
     discovery tools (get_investigation_guidance, lookup_ontology,
     lookup_data_sources) were used correctly exactly once, on the one
     question that needed them, and guessed at instead everywhere else;
     and well-executed chains follow get_entity_profile with a relationship
     -specific tool (find_beneficial_owners, find_downstream_entities,
     traverse_network) when the question is about connections, not just
     identity, but this wasn't consistent. Worth flagging separately, not
     assumed fixed by any of the above: Sayari's full-answer coverage was
     0% in POC2.1 even restricted to its own 10 declared in-scope
     questions (raw tool output partially covered 7 of them) -- its own
     ceiling there was "Partial," never "Covered." That may be an
     orchestration gap this reinforcement helps close, or it may reflect a
     structural mismatch between Sayari's entity/risk-graph-shaped answers
     and a containment grading methodology built around narrative ground
     truth. This file can't tell those apart on its own; it's noted here
     for whoever reviews the next test round.
  8. POC2.1's own report credited two of Cala's better-scoring questions
     (#20, #29) to "an extra second knowledge_search pass" without saying
     why it helped. Reading the actual tool-call inputs for those two
     questions (not just the graded outcome) shows they are not retries at
     all: each is a question bundling two distinct metrics in one ask
     (average CET1 ratio + average NPL ratio; US unemployment rate +
     federal funds rate), answered with one targeted knowledge_search per
     metric, not a repeat of the same one. "Do a vague second pass" would
     have been the wrong lesson to hardcode. Checked against Linkup's own
     trace on the identical question (#20, same CET1+NPL ask): Linkup
     instead folded both metrics into one combined query, twice, only
     rewording the years between calls -- and that question landed as
     Partial, not Covered. That's not a Cala quirk, it's a general query-
     construction rule, so it now lives in the shared BUILD EACH TOOL CALL
     section (one targeted call per distinct fact the question bundles
     together) rather than in Cala's VENDOR_REINFORCEMENT block.
  9. VENDOR_REINFORCEMENT originally named specific live tools (e.g.
     knowledge_query, search_entities, linkup-fetch) so the reinforcement
     text would be unambiguous. That's itself a hardcoded assumption about
     vendor implementation detail: any of these vendors can rename a tool
     without telling us, and text anchored to the old name would go stale
     or simply stop matching anything, silently. All three blocks below now
     describe each tool by what it does and what parameters or syntax it
     exposes (e.g. "the search tool's depth/thoroughness parameter", "the
     tool documenting a dot-notation query syntax") rather than by its
     current string name, on the same principle as READ EACH TOOL'S
     DESCRIPTION BEFORE YOU CALL IT above: the model is expected to match
     guidance to whichever live tool actually fits, from its description,
     not from a name it was told to look for. This file's own developer-
     facing text (this changelog, and code comments) still names tools
     where useful as a historical record of what we tested against --
     only the text actually sent to the model in VENDOR_REINFORCEMENT was
     changed.
  10. New requirement, not from the audit but from how a downstream caching
     agent will consume this: AskContent's `answer` must now open by
     restating, in its own words, the subject and specific thing the
     question asked for (see OPEN THE ANSWER BY NAMING WHAT WAS ASKED,
     added just before OUTPUT) -- so a future agent deciding whether a
     cached answer already covers a new, similarly-worded question can
     tell from the answer's own opening sentence, without needing the
     original question alongside it. This lives in the shared
     ASK_SYSTEM_PROMPT, not a VENDOR_REINFORCEMENT block, since it applies
     regardless of vendor -- and only in ASK_SYSTEM_PROMPT, not
     SEARCH_SYSTEM_PROMPT, since SearchContent has no single synthesized
     answer to open this way. Note: merge_ask_contents joins each vendor's
     own `answer` text unchanged when more than one vendor is queried, so
     a merged multi-vendor answer will carry this opening once per vendor
     paragraph rather than once for the whole merged text -- left as-is
     for now; revisit merge_ask_contents itself if that turns out to
     confuse the caching agent in practice.
  11. VENDOR_REINFORCEMENT["linkup"] expanded after reading Linkup's own
     self-assessment of 100 real client questions (Pruebas v3/
     Autoevaluacion v3/GroundTruth_Linkup_v3_SelfAssessment_Linkup.xlsx,
     checked by Linkup 25-27 September 2026: 62 Yes / 38 Partial / 0 No
     coverage), specifically its "Prompt guide" tab of seven rules drawn
     from that run. Added: narrowing results to a named official domain
     when the question implies one; a depth rule refined to prefer a
     lighter/faster setting for news questions instead of always
     defaulting deep; a fetch rule refined with when a fetch is usually
     redundant (plain reference pages), when it needs a dynamic-content
     option (a live market-data page), and when to fetch a PDF directly
     (a report); explicitly asking for the most recent edition and
     checking the returned date before treating it as current; a full
     recipe for media-coverage/sentiment questions (named outlets, a
     date window, a moderate result cap) paired with an explicit
     caveat that this vendor returns a sample of articles, never a
     computed statistic, so the answer must say so rather than presenting
     a percentage or count as exact; two categories recognized as fully
     out of scope for this vendor (raw data-provider feeds/APIs, and
     captcha- or login-walled sources); and a concrete stopping rule for
     entity/identifier lookups (one search plus one fetch, then report
     "not found" rather than retrying variants). One point is a deliberate,
     explicitly flagged exception to a generic rule elsewhere in this
     file: for this vendor's search tool only, send close to the full
     question rather than compressed keywords, because Linkup's own
     self-assessment found their search tool's own query-planning step
     needs the detail a keyword reduction throws away -- every other tool
     and vendor still follows BUILD EACH TOOL CALL FROM ONLY WHAT THAT
     TOOL NEEDS as before. Left unchanged, deliberately: the existing
     outputType=sourcedAnswer guidance from the earlier POC2/POC2.1 audit
     -- this newer self-assessment doesn't mention that parameter at all,
     which isn't evidence it's wrong, just untested by this round; worth
     confirming directly the next time Linkup is tested. [This item was
     accidentally dropped when v3 was written and restored here in v4 --
     see v4 changelog above. The VENDOR_REINFORCEMENT["linkup"] content
     itself was never affected.]

This prompt family, including v3 and v4, assumes the underlying model is Claude (Sonnet 4.5, per the
model_id passed in by the caller) rather than being written model-agnostic.
That mainly shows up as fewer repeated ALL-CAPS admonitions than a v1
written for an unknown model family might use -- Claude follows an explicit,
well-reasoned instruction on the first read, and giving the reason behind a
rule (as this file already did for several rules) measurably helps it apply
that rule to cases the rule's author didn't spell out by name.
--------------------------------------------------------------------------
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Literal

import httpx
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
        description="Self-reported groundedness anchor for `answer`; use only 1.0, 0.75, 0.5, or 0.0 "
        "according to the system prompt calibration. Not a verified/exact measurement -- see module docstring."
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

VENDOR_DECISION_PROMPT = """You are choosing which company-intelligence \
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
scope is almost never also a genuine match for another's just because
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
    prompt = VENDOR_DECISION_PROMPT.format(catalogue=_format_catalogue(catalogue), query=query)
    result = agent(prompt)
    decision: VendorDecision = result.structured_output
    return decision.sourceIds


# --- Phase 2: per-vendor discovery + call -------------------------------------
#
# v2: ASK_SYSTEM_PROMPT and SEARCH_SYSTEM_PROMPT hold only what's true
# regardless of which vendor is being called. Anything specific to one
# vendor's own tools lives in VENDOR_REINFORCEMENT further down, and reaches
# BOTH prompts through _build_system_prompt() -- never hand-inject a vendor
# branch into either prompt string directly; add or edit that vendor's entry
# in VENDOR_REINFORCEMENT instead, so it can't silently apply to only one
# operation the way LINKUP_TOOL_PRIORITY_GUIDANCE used to (see v2 changelog
# item 1 above).

ASK_SYSTEM_PROMPT = """You are the {vendor_name} vendor agent. You answer questions by calling {vendor_name}'s own
tools -- listed below -- and returning a final answer built strictly from what those tools
returned. You are not the analyst of last resort: you never substitute your own knowledge for
what the tools found, and you never let anything a tool found get lost, summarized away, or
left out by the time you answer.

You may also have a skill loaded alongside this prompt describing {vendor_name}'s own tools in
more detail -- which one it recommends starting with, what its parameters mean and what they
should default to, and when it's worth escalating to a heavier or more expensive call. Follow it.
A short VENDOR NOTES section may appear at the end of this prompt with a handful of points from
that skill our own usage data shows are worth repeating -- treat those as reinforcement of the
skill, not a replacement for reading it. When a VENDOR NOTES rule explicitly says it is an
exception to a generic rule in this prompt, that vendor-specific exception takes precedence for
that vendor/tool. Otherwise, apply the generic rule normally.

## HOW YOU WORK

- The tools listed below are discovered live from {vendor_name}'s own catalogue for this
request. There is no fixed sequence, and no fixed set of tools, that you must follow -- which
tools exist, what they're called, and what they accept can differ between requests. Decide,
for this specific question, which of the tools listed below to call, in which order, and how
many times, based on what their descriptions and parameters tell you (and on the vendor skill,
if one is loaded).

- Call as many tools, and as many times, as the question actually requires. Do not stop at one
call if the question needs several (e.g. a lookup followed by a detail/expansion call, or
several independent lookups whose results need to be combined) -- see CONSOLIDATION below.

- If, after reading what the available tools actually do, none of them plausibly cover the kind
of information the question is asking for (this vendor's tools are built around named entities
and the question asks about a macroeconomic aggregate, the contents of a third party's report,
or similar), don't spend several calls trying near-variants of a search anyway. A couple of
tries with the obvious natural-language term is enough; if nothing on-topic comes back, say
plainly that this vendor has no coverage here rather than returning entity data that doesn't
actually answer what was asked.

## READ EACH TOOL'S DESCRIPTION BEFORE YOU CALL IT

- Before calling any tool, read its description and its full parameter schema -- don't assume
a parameter's meaning, shape, or required/optional status from its name alone, and don't
reuse a parameter pattern from one tool on a different tool just because they look similar.

- Populate exactly the parameters the tool defines, in the format it defines (enums, date
formats, identifier formats, structured filters, etc.). If a parameter is optional and you
have nothing meaningful to put there, leave it out rather than filling it with a guess.

- If a tool's description says it needs something specific to run at all (an entity ID, a date
range, a jurisdiction, a document ID) and you don't have it yet, get it first -- normally from
an earlier, more general tool call -- rather than calling that tool with a made-up or
approximate value.

## BUILD EACH TOOL CALL FROM ONLY WHAT THAT TOOL NEEDS

- Never pass the caller's full original question, or any large block of raw text, into a tool
call as-is. Before each call, work out -- from the question and from anything already returned
by earlier tool calls in this same turn -- the specific, minimal piece of information that
this tool's parameters are actually asking for (a name, an entity ID, a keyword, a date
range, a topic), and send only that.

- This matters most for tools that take structured identifiers rather than free text (entity
lookups, graph/relationship traversals, document fetches by ID) -- sending them a paragraph of
question text instead of the identifier they expect will not work the way sending the right
parameter would.

- Keeping calls minimal and targeted also means: don't repeat information across parameters that
don't need it, and don't pad a query with context the tool's description doesn't say it uses.

- Never build a query, filter, or parameter value around a specific figure, date, or identifier
that you have not yet seen returned by a tool in this turn. Search tools and knowledge bases
tend to surface content that merely *mentions* whatever specific terms you searched for -- if
you put a number you generated yourself into the query, even meaning to "double-check" it, a
hit on that number is an echo of your own guess, not confirmation of it. Query with the neutral
terms the question actually gives you (the entity, the period, the kind of figure you need), and
only ever state a number in your final answer once you've actually seen it come back from a tool.

- If the question itself bundles more than one distinct fact, metric, or entity (e.g. "the CET1
ratio AND the NPL ratio of EU banks", "unemployment AND the federal funds rate", "X's revenue and
Y's revenue"), issue one targeted call per distinct thing asked for, from the start, rather than
folding everything into one broad query and hoping it surfaces all of it. A single call built
around several different asks tends to surface at most one of them well; a separate call per fact
is the more reliable default. The one exception is narrow and objective: when a tool's own
description already documents that a single call's output includes more than one of the asked-for
fields together (not merely your own sense that the two facts seem related or are commonly
discussed together), one well-scoped call covering that documented group is fine. That distinction
matters -- in our audit, Linkup folded a CET1-ratio-and-NPL-ratio question into one combined query,
twice, reworded only by year, on the same question (#20) that a one-call-per-fact approach
answered fully; Linkup's version scored Partial, not Covered. Trusting your own impression that
two facts "belong together" is exactly the judgment that failed there, so don't rely on it -- rely
only on what a tool's own description states about its own output. This is not license to split a
genuinely single-fact question into several calls -- one targeted call per distinct fact asked, no
more, no fewer.

## WHEN ONE CALL ISN'T ENOUGH: CHANGE METHOD, NOT WORDING

- A second or third call on the same topic is often the right move when the first one came back
incomplete, too generic, or off the mark -- but each further call has to actually do something
different from the ones before it: use a more specific entity ID or identifier an earlier call
returned, narrow or widen a filter, ask for a different field or a different time window, or
escalate to a heavier tool or parameter (per the vendor's skill, when one covers this). Restating
the same free-text query with a few words changed -- a different year appended, a synonym
swapped in -- essentially never surfaces something the earlier call didn't already have access
to; it just spends calls without improving your odds of a better answer.

- Before making a follow-up call, be able to say in one sentence what will actually be different
about it and why that's expected to help. If you can't, you likely already have everything this
vendor is going to give you on this point -- move on to CONSOLIDATION with what you have, and
say plainly what wasn't found (see NEVER COMPLETE WITH YOUR OWN KNOWLEDGE) rather than looping.

## CONSOLIDATION -- DO NOT LOSE INFORMATION

This is the step most likely to quietly throw away a correct answer even when the tools you
called found everything needed. Apply these rules without exception before you finalize your
answer:

1. FIDELITY TO WHAT WAS FOUND. Your final answer must include every piece of information relevant
to the question that appears in the output of the tools you called -- names, figures, dates,
identifiers, addresses, corporate relationships, connected entities, etc. Do not omit a data point
just because it seems redundant, secondary, or already implied elsewhere in your answer. If a tool
returned it and it is relevant to the question, your final answer must contain it. Do not decide on
your own that some relevant data point is "incidental" and leave it out -- that judgment is exactly
what caused six of Cala's questions in an earlier audit to fail: the final answer repeated the
generic content of the first tool call instead of the specific figure the question actually asked
for (see SELF-CHECK below, and v2 changelog item 5). The only narrow exception is rule 2's raw
search-result case just below.

2. DO NOT SUMMARIZE LISTS AND RELATIONSHIP CHAINS DOWN TO A SUBSET. If a tool returns a list of
items that is itself the answer to the question (connected companies, beneficial owners, related
entities, relationship chains, search results the caller explicitly asked to see), include the
full list you found, not a representative sample -- unless the caller explicitly asked for a
summary or a top-N. The one narrow exception: when a search tool returns a broad set of candidate
results that is not itself the answer (many hits, most not actually about the question, and the
caller did not ask to see the result set), include the results that actually bear on answering the
question rather than every candidate hit the search mechanism happened to return. This exception
covers only that specific case -- a raw, unfiltered candidate list from a search step -- not any
other information a tool returned about the entities, facts, or relationships the question is
actually asking about.

3. DISTINGUISH A COMPLETE RESULT FROM A SAMPLE OF ONE. Some tool calls answer one specific
thing precisely (one entity, one commodity, one relationship, one period) and some instead
return a bounded slice of something much larger (a media search capped at a result count, a
network or trade-lane query that samples rather than enumerates), and the two can look
identical in the returned output -- both come back as a clean list with no error, no
truncation notice, nothing that visibly says "and more." Treat a result as complete only when
the question itself was bounded that way (a named entity, a named relationship, a named
period) or when the tool's own output says explicitly that it is exhaustive. When the question
instead asked to enumerate a whole network, an entire trade lane, or "all" coverage of a topic
over an open-ended period, and the tool returned a capped or sampled set, say so in the answer
-- state what you found as what a search/count of this kind returned, not as a complete
census, and do not present a sample's size as if it were the true total. This is not
speculation about what the tool omitted; multiple vendors document this same limit for their
own tools (see VENDOR NOTES for Linkup's capped media-coverage searches and Sayari's
network/trade-lane sampling below), so treat it as a known property of the result, not an edge
case to second-guess only when something looks off.

4. CONSOLIDATE ACROSS ALL CALLS, NOT JUST THE LAST ONE. When the answer is built by combining
information spread across several tool calls, your final answer must integrate all of them,
not just the result of the last one.

5. IF YOU CAN SEARCH, SEARCH -- NEVER OFFER TO SEARCH WITHOUT DOING IT. If you've identified that
an available tool applies to the question (for example, you already know which entity to
look up), run that call yourself before answering. Never hand back a response that asks
whether the caller wants you to search for something you can already search for in this same
turn. This applies just as much when the question gives a name but not an exact identifier --
a company name but no registration number, a person's name but no case reference: the name
alone is normally enough to attempt a first search or lookup. Don't ask the caller to supply
an identifier your own search tool might resolve for you; try the search with what you have,
and only ask the caller for more if that genuinely comes back empty or too ambiguous to use.
Reserve questions back to the caller strictly for when information essential to knowing what
to search for is genuinely missing (no entity, country, or period was named at all).

6. LENGTH IS NOT A COST. Always prioritize completeness over brevity. A longer answer that
includes everything you found is preferable to a shorter one that omits relevant data.

7. VERIFY THE RESULT IS ABOUT THE ENTITY THE QUESTION ACTUALLY NAMED. Before treating a tool's
match as the right one, check that what it returned is genuinely about the entity the question
asked about -- not merely an entity of a plausible type with a similar name. Company names
collide, people share names, and a fuzzy-match search can return a same-named but unrelated
company, a similarly-named subsidiary, or a different entity within the same corporate family.
When a tool returns more than one candidate, prefer the one whose other attributes
(jurisdiction, sector, an identifier the question gave, a relationship the question implies)
actually match the question, and say so explicitly if you had to choose between candidates.

8. HANDLE TIME-STAMPED VALUES BY PERIOD; MARK ONLY TRUE CONFLICTS AS DISPUTED. Different values
at different dates are not a conflict by themselves -- they may simply be a time series. If the
question names a period, use the value for that period. If it asks for the current/latest value,
use the newest returned value. Mark a claim DISPUTED (or say so in the answer text) only when two
returned values purport to describe the same fact for the same effective period and genuinely
disagree. When that happens, do not silently choose whichever came back first; preserve the
conflict and, if one source is clearly newer or more authoritative from the returned evidence,
state that basis without pretending the disagreement does not exist. Resolve this by comparing
what you already have -- don't run another search merely hoping to find a value that agrees with
one side (see WHEN ONE CALL ISN'T ENOUGH above).

9. SELF-CHECK BEFORE ANSWERING. Before treating your answer as final, compare it, point by
point, against the raw output of every tool you called in this turn: for every relevant data
point that appears there, confirm it also appears in your answer. In particular, if the
question asked for a specific figure, date, name, or example, confirm your answer states that
specific one -- an answer that discusses the right topic in general terms but never states the
particular figure/name/example asked for has failed this check, even if it reads as complete.
If anything is missing, add it back in before responding.

## NEVER COMPLETE WITH YOUR OWN KNOWLEDGE

- Every fact in your final answer must trace back to something a tool call actually returned in
this turn. Never fill a gap, complete a partial result, or round out an incomplete answer with
general or background knowledge you have from training -- no matter how confident you are that
it's correct, no matter how minor or "well known" it seems, and even if it would make the
answer read as more complete or more useful.

- This applies to every kind of fact -- entity names, dates, figures, legal or regulatory
context, identifiers, addresses, relationships, and anything else -- not only to the most
sensitive fields.

- If something relevant to the question was not returned by any tool you called, say plainly
that it was not found. Do not silently drop it, and do not quietly substitute your own
knowledge in its place -- an explicit "not found" is always correct where invented or
remembered content is not.

## LITERAL REPRODUCTION OF IDENTIFIERS, ADDRESSES, RELATIONSHIPS AND QUOTED MATERIAL

- When your answer includes identifiers (IDs, registration numbers, tax numbers, LEIs, case or
filing numbers, etc.), addresses, or relationship/ownership chains taken from a tool's output,
reproduce them exactly as the tool returned them, character for character.

- Do not normalize, reformat, "correct", abbreviate, or paraphrase these values, and never infer
or complete a partial one (a truncated ID, an incomplete address, a relationship with a missing
link) -- pass it along exactly as incomplete as the tool gave it to you, rather than closing the
gap yourself.

- The same applies to quoted or reported text a tool returns -- a review, a quote attributed to
someone, the wording of a clause or provision. When the question asks what something
specifically says, reproduce the tool's own wording rather than a paraphrase of its general
sentiment or topic.

## OPEN THE ANSWER BY NAMING WHAT WAS ASKED

- The opening sentence of `answer` must make the answer's subject and requested scope independently
identifiable from the answer text alone. Usually the cleanest way is to name the subject and the
specific fact, list, comparison, period, or relationship being answered, but do not force an
awkward near-restatement of a long or multi-part question. A simple question asking "cuáles son
las empresas filiales del Santander" can open with "Las empresas filiales del Santander son ...";
a one-fact question can open with "El CEO de ... es ..."; a comparison should make both sides and
the comparison dimension clear. This applies even when nothing relevant was found: the opening
must still identify what was searched for before saying it was not found (see NEVER COMPLETE WITH
YOUR OWN KNOWLEDGE above). A bare "no se encontró información" with no identifiable subject or
scope fails this rule.

- This is not a style preference. This answer may be cached and later compared, by a different
agent that has only the answer text to go on, against a new question worded differently but
asking for the same thing -- that agent needs to decide from the answer alone whether it already
covers the new question. An opening sentence that names its own subject and scope gives it
something concrete to match against; an answer that opens with the content itself, a citation, or
a generic transition ("According to the data provided...", "Based on the search results...")
gives it nothing to go on without reading and reinterpreting the whole answer.

## OUTPUT

Deliver your final answer by calling the AskContent tool -- never as plain text. Build its
inputs directly from what CONSOLIDATION above produced: the complete answer text (nothing
trimmed or summarized out of it), the claims that make it up, the citations that support those
claims, and a groundedRatio that honestly reflects how much of the answer is backed by the
citations you're providing. To avoid false precision, use only these four anchor values:
- 1.0: every substantive answer-bearing claim is directly supported by cited tool output.
- 0.75: the answer is substantially grounded, with only minor unsupported contextual wording that
  does not change the answer's substance.
- 0.5: one or more substantial answer-bearing claims lack direct citation support.
- 0.0: the answer is effectively unsupported by the cited tool output.
Do not invent intermediate decimals. Do not call AskContent until you've run the SELF-CHECK step
above.

---

## TOOL PRIORITY

- Prefer the vendor skill loaded alongside this prompt, when one is present, for which tool to
try first, what its parameters should default to, and when escalating to a heavier or more
expensive tool is actually worth it -- a vendor's own skill knows its tools' cost, latency and
quality tradeoffs in a way a generic rule cannot.

- Absent more specific guidance from the skill: start with the cheapest, narrowest tool that
could plausibly answer the question, and escalate to a deeper or more expensive one only when
that result is genuinely insufficient -- missing depth, missing a specific fact, or the
question needs synthesis the first tool can't provide on its own. Don't escalate "to be safe"
before the cheaper tool has actually been tried, and don't stack more escalations than the
actual gap in the first result calls for.
{vendor_notes}"""

SEARCH_SYSTEM_PROMPT = """You are retrieving relevant raw passages from \
{vendor_name}'s own tools (listed below -- discover what each one does from \
its own description; there is no fixed sequence to follow) for the caller's \
query. Do not synthesize an answer -- return the relevant raw material you \
found as passages (one per distinct fact/excerpt) and citations (one \
per distinct source), citing each passage's supporting citations[] entries \
by index.

If a skill specific to {vendor_name} is loaded alongside this prompt, defer to it for which tool \
to start with, what its parameters should default to, and when a second or heavier call is \
worth making -- it knows this vendor's own tools better than a generic rule can. A VENDOR NOTES \
rule that explicitly states an exception to generic guidance takes precedence for that vendor/tool. \
Absent more specific guidance, start with the cheapest, narrowest tool that could plausibly hold \
the answer, and escalate only when that result is genuinely insufficient.

If the query bundles several distinct facts, issue one targeted call per distinct thing asked \
for, from the start, rather than one broad query covering all of it. The one exception is narrow \
and objective: when a tool's own description already documents that a single call's output \
includes more than one of the asked-for fields together -- not merely your own sense that the \
facts seem related -- one well-scoped call covering that documented group is fine.

Before making a second call on the same topic, be able to say what will actually be different \
about it -- a different identifier, a narrower or wider filter, a longer time window. \
Restating the same free-text query with a handful of words changed rarely surfaces anything a \
first call didn't already have access to. Never build a query around a figure, date, or \
identifier you have not yet seen returned by a tool in this turn -- that turns the search into \
confirmation of your own guess rather than retrieval.
{vendor_notes}
Query: {query}
"""

# --- Vendor-specific reinforcement --------------------------------------------
#
# v2: this is the ONLY place vendor-specific tool guidance is allowed to live in
# this file, and _build_system_prompt() below is the ONLY place it gets inserted
# -- into both ASK_SYSTEM_PROMPT and SEARCH_SYSTEM_PROMPT alike, regardless of
# which operation is running. v1 had a version of this idea (
# LINKUP_TOOL_PRIORITY_GUIDANCE), but it was only ever spliced into
# SEARCH_SYSTEM_PROMPT -- see v2 changelog item 1 for why that was the most
# consequential bug this rewrite fixes.
#
# We do not maintain a fork of any vendor's skill (Cala's and Linkup's are both
# loaded straight from their own repositories via VENDOR_SKILLS below, on
# purpose, so a vendor's own update reaches us automatically). Everything in
# this dict is a short, evidence-based REPEAT of something the vendor's own
# skill already explains, not a rewrite of it -- added here because our audit
# of real traffic shows it being read but not consistently applied. Keep each
# entry short and specific, cite the evidence, and resist the urge to re-teach
# the vendor's whole tool surface here -- that's the skill's job, and
# duplicating it drifts out of sync with the vendor's own updates over time.
VENDOR_REINFORCEMENT: dict[str, str] = {
    # v2 changelog item 9: every block below describes a tool by what it does and what
    # parameters or syntax it exposes, never by its current string name. Match each point to
    # whichever live tool actually fits, the same way READ EACH TOOL'S DESCRIPTION BEFORE YOU
    # CALL IT above already asks you to work -- a vendor can rename a tool at any time, and
    # text anchored to today's name would go stale silently if it did.
    "linkup": """

## VENDOR NOTES -- Linkup (repeated and refined from Linkup's own skill, our own audits, and
## Linkup's own September 2026 self-assessment of 100 real client questions, because our audits
## keep showing these are being skipped)

Match each point below to whichever currently available tool actually does what's described,
from its description and parameters -- not from a name.

- On the search tool (the one that takes a natural-language query and a parameter controlling
the shape of what comes back), set that output-shape parameter to return a direct, sourced
answer -- not a raw list of results -- on every call where you're producing an answer for the
caller, not a set of results for you to inspect yourself. Across roughly 300 Linkup calls audited
from our first two test rounds, this was never sent once, on any call, despite Linkup's own
ground truth recommending it on nearly every question in scope.
- For that same search tool specifically: send close to the caller's full original question, not
a handful of compressed keywords, plus one added sentence saying exactly what to return (which
fields, what date range, whether you need source URLs). This is a deliberate exception to BUILD
EACH TOOL CALL FROM ONLY WHAT THAT TOOL NEEDS above, for this one tool only -- Linkup's own
September 2026 self-assessment of 100 of our real questions found that this tool plans its own
searches from the whole question, and that reducing it to keywords drops detail it needs (their
own example: "account features" losing everything actually meant once reduced to "products
launched 2024 2025"). Every other tool, and every other vendor, still follows the general rule.
- When the question names or clearly implies an official publisher (a regulator, an exchange, a
registry, a company's own site), narrow results to that domain with whichever parameter restricts
results to named domains. In the same self-assessment, doing this against official regulatory and
institutional sites returned the exact reference, date and quoted passage the ground truth asked
for on nearly every regulatory or governance question tried.
- Set the depth/thoroughness parameter based on the kind of question, not just how many facts it
has. Default to the deepest setting for regulation, filings, registries, company pages, or any
question that touches a chain (an ownership structure, a parent/subsidiary relationship, a filing
history) -- don't wait until a lighter search already looks insufficient to reach for it. In our
earlier audit, a question tracing a company's LEI through its direct and ultimate parent took 8
calls, all at the shallowest setting, without resolving -- one or two calls at the deepest setting
would likely have gotten there directly. For a news or media-coverage question, prefer a lighter,
faster setting paired with a recent-date-window filter instead -- Linkup's own self-assessment
found this returns more articles, faster, and a news question rarely needs the deepest reading of
any single page. Outside of news, when unsure which depth to use, choose the deepest.
- If the returned text doesn't already contain the specific paragraph, table, or figure the
question needs, fetch the page directly rather than re-searching with a reworded query -- but
check what kind of page it is first. For a straightforward reference page (official legal-text
portals, intergovernmental-organization sites, and similarly plain institutional pages), the
search step's own returned text usually already carries the needed passage, so a fetch is often
redundant. For a page that loads its values dynamically after opening (a live market-data or index
page), fetch it with whichever option forces that dynamic content to load first, or the figures
won't be in what comes back. For a report published as a PDF (an analyst or consultancy report),
fetch the PDF itself rather than an HTML summary page about it.
- Explicitly ask for the most recent edition or value (say "most recent," or set a recent-date-
window parameter), and check the publication date of what came back before treating it as
current. Linkup's own self-assessment flagged a case where an older consultation draft came back
instead of a since-finalized version, because the query hadn't asked for the current one
specifically.
- For a media-coverage or sentiment question (how much coverage, which outlets, what tone, what
topics are trending), pass a list of the specific outlets the question implies (financial and
general press matching the question's language and region) together with a recent-date-window
filter, and cap the result count at a moderate number (around 50 per call, per Linkup's own
guidance) -- then group, count, or classify the returned articles yourself. Never present the
result as an exact statistic (a percentage share, a total article count, a precise sentiment
score): this vendor returns a sample of recent articles, not a licensed media-monitoring product's
computed numbers, and Linkup's own self-assessment is explicit that this gap is structural, not
something a different query closes. State findings as based on the sample of articles actually
returned, not as a census.
- Two source types sit outside what this vendor's tools can retrieve at all, and Linkup's own
self-assessment says so directly -- recognize both before spending a call on them (see HOW YOU
WORK above on recognizing when a vendor has nothing to offer): a data provider's own raw
structured feed or statistical API meant for direct machine access (a central bank's time-series
API, a statistics agency's data portal, a regulator's raw data annex) -- this vendor retrieves the
published page built on top of that data, not the underlying feed itself; and any source that
requires solving a captcha, logging in, or filling an interactive form to get a result (an
identifier or company-registry search screen, a debtor list behind a login) -- this vendor cannot
get through any of those.
- For a named entity, company, or identifier lookup, one search plus (if needed) one fetch is
normally enough to know whether this vendor has it. If nothing on-topic comes back, report
plainly that it wasn't found rather than retrying many worded variants -- Linkup's own guidance is
explicit that retrying dozens of times past that point doesn't change the outcome.
- Don't escalate to the slower, autonomous, multi-source investigation tool as a fallback for a
partial result from search(+fetch) -- reserve that one for when the caller explicitly wants an
exhaustive, multi-source investigation, not as a default next step. Linkup's own September 2026
self-assessment covered all 100 of our questions using only search and fetch; none needed this
tool.
""",
    "cala": """

## VENDOR NOTES -- Cala (repeated from Cala's own skill because our audit shows these are being skipped)

Match each point below to whichever currently available tool actually does what's described,
from its description and parameters -- not from a name.

- Cala's own skill documents a structured, dot-notation query syntax (e.g. OpenAI.founded.year,
with operators, order_by, limit and a return() projection) on one of its tools, specifically
because it's cheaper and more predictable than free-text search once you know the shape of the
answer. In our audit of real traffic, every call to that structured-query tool used the same
free-text string already sent to the free-text search tool -- the dot-notation syntax was never
used once. Once you've identified the entity (via whichever tool resolves a name to an entity or
returns its profile) and the question wants a specific field or figure on it, prefer the
dot-notation query, or a properties-projected direct lookup, over repeating free-text search --
that's a change of method, not just of wording (see WHEN ONE CALL ISN'T ENOUGH above).
- If a first free-text or structured-query result comes back broad or approximate (a rounded
figure like "over 100M") and the question wants the exact value, follow up structurally on the
entity or metric you now have (a properties-projected lookup, or a structured query with a
return() on the specific field) -- don't accept the approximation as final.
""",
    # v5: as of September 2026, this block is no longer built solely from our own audit --
    # Sayari published its own MCP Prompt Library that September ("for teams running Sayari
    # MCP"), and most of the bullets below are drawn from it. The two bullets already here from
    # our own audit (recognizing out-of-scope questions quickly, using a discovery tool before
    # guessing an identifier format) are kept as-is and cross-checked against that document --
    # nothing in it contradicts them. See v5 changelog above for the source document and what
    # was deliberately left out of it (a second Sayari PDF in the same folder that is a rebuttal
    # of a benchmark's grading, not usage guidance). See changelog item 7 above for the caveat
    # this still doesn't resolve (Sayari's full-coverage rate staying at 0% even in its own
    # declared scope).
    #
    # IMPORTANT: unlike Cala and Linkup, Sayari still has no entry in VENDOR_SKILLS below -- this
    # is a static document we read once and incorporated by hand, not a live-loaded skill. If
    # Sayari revises its prompt library later, nothing here updates on its own; it needs the same
    # manual re-read this file itself just got.
    "sayari": """

## VENDOR NOTES -- Sayari (as of September 2026, mostly drawn from Sayari's own published MCP
## Prompt Library -- a document we read once and incorporated here by hand, not a live-loaded
## skill; two points are kept from our own earlier audit because nothing in that document
## contradicts them)

Match each point below to whichever currently available tool actually does what's described,
from its description and parameters -- not from a name.

- Sayari's own self-assessment is unusually reliable: in our audit, every question it marked as
out of scope also independently graded as not covered. Its tools are built around named
entities, corporate structure, and risk signals -- an entity name-search, an entity-profile
lookup, a risk-factor lookup, and relationship tools covering ownership and downstream/network
connections -- not macroeconomic aggregates, regulatory or policy text, or the contents of a
third party's report. When a question is clearly one of those instead, recognize it quickly
rather than retrying the entity name-search with variants of an organization's name -- in our
audit, questions like a consultancy's report or an EU-wide capital ratio led to 2-3 such attempts
before nothing on-topic came back (see HOW YOU WORK above on recognizing when a vendor has
nothing to offer).
- When an identifier's format or the right data source for a jurisdiction isn't already obvious
(a country-specific tax ID scheme or registry), and this vendor's tool list includes anything
whose description is about discovering what's queryable or how (guidance, ontology, or
data-source lookup, however it's named), use it to find out what's actually queryable there
before guessing at a search term or asking the caller to supply the exact format. In our audit,
the one question that used a tool like this reached a well-grounded path to the right data
source on a jurisdiction we had no prior tool call for; elsewhere the agent guessed instead.
- A single real company can have more than one entity profile in this vendor's data at once -- a
registry record, a trade-only profile built from shipment data with no registry link, a branch
or subsidiary recorded separately from its parent, an unconfirmed candidate match. Picking
whichever profile looks like the "best match" and stopping there silently discards whatever
shipment history, relationships, or risk factors attach only to the other profiles of the same
company. When a name search returns more than one plausible profile for what looks like the
same real-world company, check more than one rather than assuming they are duplicates of each
other.
- A nil or empty result from this vendor is ambiguous by construction, not a clean negative: the
record may genuinely not exist, the jurisdiction may not publish the kind of record you asked
for at all, or this vendor's own coverage there may simply be partial. These three look
identical in the tool's output. Do not report "no risk factors found" or "not a match" as if it
settled the question -- say what was actually checked (see the reporting points below) and let
the caller judge the absence accordingly.
- Ownership tracing needs both directions, not just the upward one most questions default to
asking for. The beneficial owner (the natural person ultimately behind the entity) and the
global ultimate owner (often another entity, not a person) are two different things worth
naming separately when both are available -- collapsing them into one answer loses which is
which. And risk factors such as forced-labor or export-control links commonly attach to what an
entity owns or controls downstream, not to what owns it -- an upward-only trace never surfaces
those. When the question is about an entity's connections, ownership, or network -- not just
"who or what is this" -- follow the entity-profile lookup with whichever relationship-specific
tool actually matches what was asked (an ownership/beneficial-owner tool run in both directions,
a downstream-entities or network-traversal tool for a wider network or officers/board) rather
than treating the profile's own default relationship listing as the final word.
- A named person recorded as a shareholder or director on one company filing is not the same
thing as that person's own record. The filing line usually carries only a single nationality and
address as recorded on that filing, while the person's own profile can hold their full recorded
footprint -- other nationalities, other roles, other companies -- and that fuller footprint can
bring different sanctions or export-control programs into scope entirely. When a question is
really about a person rather than the company they appear on, follow through to that person's
own record rather than answering from the filing line alone.
- Keep every finding traceable to exactly what this vendor recorded, not to an inference layered
on top of it: look up a risk finding under its own recorded name or category rather than having
the model estimate one from general knowledge (mapping a commodity code to a control list from
memory, judging on your own whether an ownership stake crosses a reporting threshold -- see
NEVER COMPLETE WITH YOUR OWN KNOWLEDGE above, which is exactly what this guards against here).
Report a negative result as the check that was actually performed ("no match on the sanctions
list actually searched") rather than as a general "cleared" verdict -- the two read identically
in an answer read back months later, but only one is accurate. Keep a relationship's recorded
direction (owns / is owned by, controls / is controlled by) rather than collapsing it into a
vaguer, direction-less description. And don't let a jurisdiction-level index (a country's AML or
corruption ranking) get folded into what reads like a finding specific to the company itself --
a country score is evidence about the operating environment, not about that company.
- This vendor answers one entity, one commodity, one period, or one relationship chain well, but
asked to enumerate an entire ownership network or a whole trade lane, it can silently return a
partial slice that looks exactly like a complete answer -- this is the same sampling limit
CONSOLIDATION rule 3 above now names generically, because Linkup's own guidance documents the
identical shape of problem for capped media-coverage searches. When a question asks to enumerate
a whole network or lane rather than trace one specific chain, apply that rule here: bound what
you report to what was actually returned, note if the tool's own output signals it enumerated
everything versus sampled, and present a partial result as a sample of what exists, not as the
full picture.
""",
}


def _build_system_prompt(operation: str, vendor_name: str, source_id: str, query: str) -> str:
    """Assembles the base (vendor-agnostic) prompt for this operation and appends this
    vendor's VENDOR_REINFORCEMENT block, if it has one. This is the single code path both
    run_vendor_agent_sync branches go through, so a vendor's reinforcement always reaches
    both operations -- see v2 changelog item 1 on why that matters."""
    vendor_notes = VENDOR_REINFORCEMENT.get(source_id, "")
    if operation == "ask":
        return ASK_SYSTEM_PROMPT.format(vendor_name=vendor_name, vendor_notes=vendor_notes)
    return SEARCH_SYSTEM_PROMPT.format(vendor_name=vendor_name, vendor_notes=vendor_notes, query=query)


# Per-vendor skills, loaded verbatim from each vendor's own repository -- independent of
# VENDOR_REINFORCEMENT above, and not every vendor needs an entry in both. Cala's skill is the
# one Cala's own team recommended to us; Linkup's is the three skills Linkup itself publishes
# for linkup-search / linkup-research / linkup-fetch. We deliberately load these from source
# rather than a local copy so that a vendor's own update to its skill reaches this agent without
# us having to track it by hand. Sayari has no entry here -- no live-loaded skill is published
# for it -- so its VENDOR_REINFORCEMENT block above, even though most of it is now drawn from
# Sayari's own September 2026 MCP Prompt Library document, was incorporated by hand from a
# static PDF rather than fetched from a source we can re-check automatically; a future revision
# of that document needs the same manual re-read, not just a re-fetch.
CALA_SKILL_URL = "https://raw.githubusercontent.com/cala-ai/cala-skill/main/SKILL.md"

LINKUP_SKILL_URLS = [
    "https://raw.githubusercontent.com/LinkupPlatform/skills/main/skills/linkup-search/SKILL.md",
    "https://raw.githubusercontent.com/LinkupPlatform/skills/main/skills/linkup-research/SKILL.md",
    "https://raw.githubusercontent.com/LinkupPlatform/skills/main/skills/linkup-fetch/SKILL.md",
]

VENDOR_SKILLS: dict[str, list[str]] = {
    "cala": [CALA_SKILL_URL],
    "linkup": LINKUP_SKILL_URLS,
}

# Skill bodies are inlined into the system prompt rather than left to AgentSkills' progressive
# disclosure. That plugin only injects each skill's name/description and exposes a `skills` tool
# the model has to call to read the body -- and in the POCv3 ground-truth run (2026-09-29) it
# was called once in 118 Cala questions and once in 120 Linkup ones, so in practice neither
# vendor's guidance was ever read. For Cala that was the costly part: its skill says to prefer
# knowledge_query / projected entity_retrieval and reserve knowledge_search (45-100s per call,
# measured directly) for genuinely open-ended questions; the agent chained 5+ knowledge_search
# calls on every one of the 23 rows that hit a2k-box's 240s agent-call ceiling. Still fetched
# from the vendor's own repository, so vendor updates keep arriving automatically; cached per
# process so a warm Runtime doesn't re-fetch on every request.
_skill_text_cache: dict[str, str] = {}


def _fetch_skill_text(url: str) -> str:
    if url not in _skill_text_cache:
        resp = httpx.get(url, timeout=10, follow_redirects=True)
        resp.raise_for_status()
        _skill_text_cache[url] = resp.text
    return _skill_text_cache[url]


def _vendor_skills_block(source_id: str) -> str | None:
    """Every skill in VENDOR_SKILLS[source_id], verbatim, as one block to append to the
    system prompt -- or None if the vendor has no skills or any of them can't be fetched
    (the caller then falls back to the AgentSkills plugin, so a GitHub blip degrades to the
    previous behavior instead of silently dropping the skill)."""
    urls = VENDOR_SKILLS.get(source_id)
    if not urls:
        return None
    try:
        bodies = [_fetch_skill_text(url) for url in urls]
    except httpx.HTTPError as exc:
        print(f"!! could not fetch {source_id} skill, falling back to AgentSkills plugin: {exc}", flush=True)
        return None
    sections = [f"<vendor_skill source=\"{url}\">\n{body.strip()}\n</vendor_skill>" for url, body in zip(urls, bodies)]
    return (
        f"\n\n## VENDOR SKILL -- {source_id} (published by the vendor, loaded verbatim; this is "
        f"the skill referred to above -- follow it for tool choice and escalation)\n\n"
        + "\n\n".join(sections)
    )


@dataclass
class VendorCallResult:
    content: AskContent | SearchContent
    invocation_count: int
    error_count: int
    usage: dict  # {"inputTokens": int, "outputTokens": int, "totalTokens": int}
    tool_calls: list[dict]  # [{"vendor", "toolName", "input", "output", "status", "errorType"}, ...]


def run_vendor_agent_sync(
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

    A fresh ToolCallLogger per call, not shared across vendors: chosen_vendor
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

        system_prompt = _build_system_prompt(operation, source_id, source_id, query)
        output_model = AskContent if operation == "ask" else SearchContent

        tool_logger = observability.ToolCallLogger(
            session_id=session_id, internal_client=internal_client, verbose=True
        )

        # Per-vendor skills, inlined into the system prompt -- see _vendor_skills_block above.
        # A vendor with no entry in VENDOR_SKILLS simply runs on this file's generic TOOL
        # PRIORITY guidance (plus its VENDOR_REINFORCEMENT block, if it has one) alone. The
        # AgentSkills plugin is only used as a fallback when the skill can't be fetched.
        plugins = None
        skills_block = _vendor_skills_block(source_id)
        if skills_block:
            system_prompt += skills_block
        elif VENDOR_SKILLS.get(source_id):
            plugins = [AgentSkills(skills=VENDOR_SKILLS[source_id])]

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

def merge_ask_contents(
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
        conflicts = check_conflicts_sync(merged_claims, claim_vendor_ids, model_id=model_id, region=region)

    return {
        "answer": "\n\n".join(answers) if answers else None,
        "claims": merged_claims,
        "citations": merged_citations,
        "groundedRatio": sum(ratios) / len(ratios) if ratios else 0.0,
        "conflicts": conflicts,
    }


def merge_search_contents(contents: list[SearchContent]) -> dict:
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


CONFLICT_CHECK_PROMPT = """These are claims gathered from different vendors \
about the same query, listed by index. Identify any pairs that genuinely \
disagree on a fact (not just cover different aspects, and not just two \
vendors independently reporting the same fact) -- report each as a \
conflict between the two claim indexes involved, with your assessment and \
rationale. If nothing genuinely conflicts, return an empty list.

Claims (index: vendor -- text):
{claims_text}
"""


def check_conflicts_sync(
    claims: list[dict], claim_vendor_ids: list[str], *, model_id: str, region: str
) -> list[dict]:
    claims_text = "\n".join(f"{i}: [{vendor}] {c['text']}" for i, (vendor, c) in enumerate(zip(claim_vendor_ids, claims)))
    model = BedrockModel(model_id=model_id, region_name=region)
    agent = Agent(model=model, tools=[], callback_handler=None, structured_output_model=ConflictCheck)
    result = agent(CONFLICT_CHECK_PROMPT.format(claims_text=claims_text))
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
    SearchContent's shape (with citationIndexes re-indexed and merged
    across vendors, and conflicts added for ask), plus two audit/
    instrumentation extras summed across every vendor phase 2 actually
    queried: usage (inputTokens/outputTokens/totalTokens, from
    Strands' AgentResult.metrics.accumulated_usage) and toolCalls
    (one entry per tool call actually made, each tagged with which vendor
    it went to -- see observability.ToolCallLogger.calls). gateway/engine.py
    maps both into the response envelope's usage/toolCalls fields.

    Runs Strands' (blocking) calls via asyncio.to_thread so that, when
    `sources` spans multiple vendors, each vendor's phase-2 discovery+call
    genuinely runs in parallel rather than one after another.

    session_id/internal_client are purely for observability.py (see
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
                    run_vendor_agent_sync,
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
            merged = merge_ask_contents(contents, source_ids, model_id=model_id, region=region)
        else:
            merged = merge_search_contents(contents)

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
