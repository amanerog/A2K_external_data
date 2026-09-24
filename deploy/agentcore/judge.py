"""Shared LLM-as-judge helper -- used by run_ground_truth_eval.py (grades
answers right after calling a2k.ask) and grade_vendor_audit.py (grades
answers already captured in a run_vendor_audit.py .jsonl file, no new
AgentCore calls needed). Split out here once a second script needed the
exact same grading logic, rather than duplicate it a second time.

Rubric adapted from a KYC/AML vendor-evaluation "Judge agent" system prompt
(v4) -- only the scoring rubric (weighted dimensions, critical_omission,
hallucination, verdict rule) was ported here, not that prompt's tool-calling
workflow: that spec has the judge query vendors itself, via its own
query_vendor_api tool, and produce an Evaluation_Log.xlsx. Ours doesn't --
run_vendor_audit.py already collects `actual_answer` separately (via a2k.ask,
not a bespoke vendor-query tool), so this stays a single grading call over
data already in hand, same shape as before.

`expected_answer` is free-form prose written by a human, and our system's
`answer` is free-form prose synthesized by an LLM -- these will essentially
never match verbatim even when the system is factually right (different
wording, different level of detail, different ordering of facts). So each
row is graded by a second LLM call (Bedrock Converse) that compares actual
vs. expected on *factual* consistency, not string equality.

`weighted_score` and `verdict` are computed here in Python from the model's
own per-dimension scores and flags, not trusted as the model's own output --
the source prompt's own "v2 changes" section documents exactly this failure
mode (a rule stated in the prompt text that the model applying it didn't
reliably follow in practice, for critical_omission specifically). Keeping
the model's job narrow (four dimension scores + two flags + the fact lists
behind them) and doing the aggregation/threshold arithmetic deterministically
removes that whole class of inconsistency for the parts that are pure math.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

# The bare model ID doesn't work -- Bedrock rejects on-demand throughput for this model
# (confirmed live: ValidationException asking for an inference profile instead), same as
# Claude Sonnet 4.5 before it. Cross-region inference profile ID (arn:aws:bedrock:eu-west-1:
# 396961015428:inference-profile/eu.anthropic.claude-sonnet-5), account-agnostic form --
# override with --judge-model on either caller script if your account's Bedrock access differs.
DEFAULT_JUDGE_MODEL_ID = "eu.anthropic.claude-sonnet-5"

# dimension -> weight, and the threshold the weighted score (on the model's native
# 1-5 scale) must clear to avoid NOT_ACCEPTABLE on its own. 4.0 on a 1-5 scale is
# the source prompt's own "80/100" threshold, just not rescaled to 0-100 here.
_WEIGHTS = {"factual_accuracy": 0.35, "completeness": 0.35, "traceability": 0.20, "relevance_clarity": 0.10}
_NOT_ACCEPTABLE_THRESHOLD = 4.0

JUDGE_PROMPT = """You are grading how well an AI system's answer to a question matches a \
reference (expected) answer, for a company-intelligence vendor-evaluation pipeline. Vendor \
responses are free-text/narrative, not structured fields -- judge semantic equivalence, not \
literal string matching: if a fact is expressed in different words but is correct and \
complete, it counts as satisfied. Do not penalize style, phrasing, or language differences \
when the factual content is correct and complete.

Question: {query}

Reference source (where the expected answer's information originates): {source}

Reference (expected) answer:
{expected_answer}

System's actual answer:
{actual_answer}

# Scoring dimensions (score each 1-5)

Scale, same for all four:
- 5 -- Fully satisfies the criterion, no errors or omissions.
- 4 -- Almost fully satisfies it; a minor omission or inaccuracy that doesn't change the conclusion.
- 3 -- Partially satisfies it; a relevant but non-critical gap or error.
- 2 -- Low satisfaction; errors or omissions that materially reduce the usefulness of the answer.
- 1 -- Does not satisfy it; missing, incorrect, or fabricated information on a central point.

- **factual_accuracy**: every factual claim in the actual answer is correct and consistent with \
the reference. Penalize incorrect or contradictory statements.
- **completeness**: the actual answer covers all the relevant facts/elements present in the \
reference (recall). If the actual answer does not address the reference's content at all -- \
empty, off-topic, or a generic "no information found" disclaimer -- score this 1, regardless of \
whether that also triggers critical_omission below.
- **traceability**: the actual answer identifies or references where its information comes \
from (record, date, source entity), coherent with the reference source above and with what is \
claimed.
- **relevance_clarity**: the actual answer's content is directly relevant to the reference, \
without noise or unrelated content that obscures the key information. A response that does not \
engage with the reference's subject matter at all scores 1 here too.

# critical_omission (boolean gating flag, evaluated separately from the four scores above)

True only when the reference answer contains at least one high-impact finding -- a fact \
belonging to one of these six categories: sanction, PEP (politically exposed person), \
litigation/legal proceeding, adverse media, beneficial ownership/UBO, or an adverse risk \
relationship -- and the actual answer does not mention that finding AT ALL (total silence, not \
a partial or imprecise mention).

**This flag is not a general "the system failed to answer" signal.** An answer that is empty, \
off-topic, an error placeholder, or a blanket "no data available" statement is a completeness \
and relevance_clarity problem, full stop -- it only additionally becomes a critical_omission if \
the reference also happens to contain one of the six finding types above. Most questions \
(pricing, products, macro statistics, org charts, company registration details, etc.) contain \
zero high-impact findings, and critical_omission must stay false no matter how bad the answer \
is. Do not let the severity of the answer's failure pressure you into setting this flag -- \
severity belongs in the four scores above.

Evaluate in this exact order:
0. Before looking at the actual answer at all, read the reference answer on its own and \
   explicitly list every discrete high-impact finding it contains (there may be zero, one, or \
   several). Do this as a fixed first step, not as an afterthought once you've already seen how \
   the actual answer reads.
1. If that list is empty, critical_omission is always false -- there is nothing to omit. Do not \
   set it true just because other details are missing, the answer is off-topic, or the answer is \
   a "no data" disclaimer; that's a completeness/relevance_clarity issue, not a gating one.
2. If the list is non-empty, for each finding, check whether the actual answer contains ANY \
   semantically equivalent mention of it -- even if brief, differently worded, or missing some \
   detail (a date, an amount).
3. If a finding is mentioned but with an error or missing detail, that is NOT a \
   critical_omission -- score it through factual_accuracy/completeness instead. This flag is \
   reserved for a finding being completely absent, not incompletely described.
4. If at least one high-impact finding is completely absent, set critical_omission = true and \
   name it in missing_facts, in language traceable to the reference answer's own text.
5. Otherwise, critical_omission = false.
6. Self-audit before finalizing: if you set critical_omission = true, confirm missing_facts \
   names a specific finding from the six categories that appears in the reference answer's own \
   text -- not a paraphrase of "the answer didn't answer" or "the answer was off-topic." If you \
   cannot point to that finding verbatim or near-verbatim in the reference, revert \
   critical_omission to false and let completeness/relevance_clarity carry the penalty instead.

# hallucination (boolean gating flag)

True only when the actual answer asserts a specific, checkable fact (a name, date, figure, \
corporate relationship, or risk finding) that clearly contradicts the reference, or is clearly \
invented with no plausible basis. Use this sparingly -- only when the fabrication is very \
evident. The reference is usually a short excerpt, not the full universe of correct facts, so a \
detail the reference simply doesn't mention is NOT automatically a hallucination.

Evaluate in this exact order:
1. Identify any specific, checkable claims in the actual answer that go beyond what the \
   reference states.
2. For each, ask: does it directly contradict the reference (a different number, name, or date \
   for the same fact), or does it invent a precise detail with no plausible source that reads as \
   fabricated? If yes to either, it's a hallucination candidate. If it's merely additional, \
   plausible, uncontradicted information you cannot confirm is false, it is NOT a hallucination \
   (you may still lower relevance_clarity if it's noise).
3. Only set hallucination = true when you are confident the fabrication is evident, not merely \
   suspected. When genuinely unsure, default to false and note the uncertainty in justification \
   instead.
4. If true, factual_accuracy must be 1, and the specific fabricated fact(s) go in invented_facts.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{{
  "factual_accuracy": 1-5,
  "completeness": 1-5,
  "traceability": 1-5,
  "relevance_clarity": 1-5,
  "critical_omission": true or false,
  "hallucination": true or false,
  "missing_facts": "semicolon-separated list, or empty string -- when critical_omission is true, must name the specific high-impact finding(s) omitted, traceable to the reference's text; may also list other missing details unrelated to the flag",
  "incorrect_facts": "semicolon-separated list, or empty string",
  "invented_facts": "semicolon-separated list, or empty string -- required, specific, when hallucination is true",
  "justification": "2-4 sentences explaining the scores and citing the specific reference fact behind each penalty; if critical_omission is true, explicitly name which of the six finding categories was omitted and quote or paraphrase the reference text it comes from"
}}
"""


@dataclass
class JudgeResult:
    factual_accuracy: int | None = None
    completeness: int | None = None
    traceability: int | None = None
    relevance_clarity: int | None = None
    weighted_score: float | None = None
    critical_omission: bool | None = None
    hallucination: bool | None = None
    missing_facts: str = ""
    incorrect_facts: str = ""
    invented_facts: str = ""
    verdict: str = "ERROR"
    justification: str = ""


def _extract_text(response: dict) -> str:
    """Converse's `output.message.content` is a list of typed blocks, not
    always a single text block at index 0 -- confirmed live with
    DEFAULT_JUDGE_MODEL_ID's Claude Sonnet 5: it returns a `reasoningContent`
    block before the `text` block (extended thinking), which `content[0]
    ["text"]` doesn't account for and fails with a bare KeyError. Scan for
    the first block that actually has `text` instead of assuming position."""
    for block in response["output"]["message"]["content"]:
        if "text" in block:
            return block["text"]
    raise KeyError(f"no text block in Converse response content: {response['output']['message']['content']!r}")


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    # Models occasionally wrap the JSON in a ```json fence despite the
    # "ONLY a JSON object" instruction -- strip that before parsing rather
    # than failing the whole row over a cosmetic formatting slip.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def judge(bedrock_client, model_id: str, query: str, source: str, expected_answer: str, actual_answer: str) -> JudgeResult:
    """Grades one (question, vendor) pair already backed by a captured
    `actual_answer` -- no vendor-querying here, see module docstring.
    Raises on a malformed/unparseable model response; callers already wrap
    this in their own try/except (one bad row must not kill a batch)."""
    prompt = JUDGE_PROMPT.format(query=query, source=source or "(not given)", expected_answer=expected_answer, actual_answer=actual_answer)
    response = bedrock_client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        # 1500, not 800 -- confirmed live (id=75, Ali Ansari, a dense
        # multi-sanction-record dossier) that 800 truncates the JSON mid-string
        # on a long expected_answer with several critical_omission findings to
        # enumerate in missing_facts/justification, raising JSONDecodeError
        # instead of a real grade.
        # No `temperature` here -- confirmed live with DEFAULT_JUDGE_MODEL_ID's
        # Claude Sonnet 5: Converse rejects it outright ("`temperature` is
        # deprecated for this model"), unlike every earlier judge model this
        # was tested against. Omit rather than guess at a replacement
        # parameter Bedrock hasn't documented here.
        # 1500 -> 6000: confirmed live that Sonnet 5 spends part of maxTokens
        # on an internal `reasoningContent` block before the actual text --
        # at 1500 that reasoning alone consumed the whole budget, leaving no
        # text block at all (_extract_text's KeyError) or a JSON response
        # truncated mid-string. Not disabling reasoning outright (no
        # documented Bedrock field confirmed for this model yet) -- just
        # giving enough headroom for both the reasoning and the JSON to fit.
        inferenceConfig={"maxTokens": 6000},
    )
    text = _extract_text(response)
    parsed = _parse_json_response(text)

    result = JudgeResult(
        factual_accuracy=int(parsed["factual_accuracy"]),
        completeness=int(parsed["completeness"]),
        traceability=int(parsed["traceability"]),
        relevance_clarity=int(parsed["relevance_clarity"]),
        critical_omission=bool(parsed["critical_omission"]),
        hallucination=bool(parsed["hallucination"]),
        missing_facts=parsed.get("missing_facts") or "",
        incorrect_facts=parsed.get("incorrect_facts") or "",
        invented_facts=parsed.get("invented_facts") or "",
        justification=parsed.get("justification") or "",
    )

    # hallucination=true forces factual_accuracy=1, same as the source rubric's
    # step 4 -- enforced here too rather than only asked for in the prompt, in
    # case the model sets the flag but doesn't also lower the score to match.
    if result.hallucination:
        result.factual_accuracy = 1

    result.weighted_score = round(
        _WEIGHTS["factual_accuracy"] * result.factual_accuracy
        + _WEIGHTS["completeness"] * result.completeness
        + _WEIGHTS["traceability"] * result.traceability
        + _WEIGHTS["relevance_clarity"] * result.relevance_clarity,
        2,
    )

    # Verdict rule, applied deterministically rather than asked of the model:
    # NOT_ACCEPTABLE if critical_omission, OR weighted_score below threshold.
    if result.critical_omission or result.weighted_score < _NOT_ACCEPTABLE_THRESHOLD:
        result.verdict = "NOT_ACCEPTABLE"
    else:
        result.verdict = "ACCEPTABLE"

    return result


def pending_manual_review(reason: str = "no ground truth available") -> JudgeResult:
    """For a row with no expected_answer to grade against -- never fabricate
    a score (see the source rubric's own constraint on this). No Bedrock
    call made; construct this directly instead of calling judge()."""
    return JudgeResult(verdict="PENDING_MANUAL_REVIEW", justification=f"Not scored: {reason}.")


# --- Containment rubric (Judge_System_Prompt_v4(v2).md, added 2026-09-16) ---------
#
# A second, independent judge -- NOT a replacement for judge()/JudgeResult above,
# the two coexist deliberately. judge() scores the final answer alone on four
# weighted dimensions; this one asks a narrower, binary-ish question ("does
# text_to_score CONTAIN the substance of expected_answer?") and, critically, is
# meant to be run TWICE per question -- once against the vendor's raw tool
# output, once against the consolidated final answer -- so a "consolidation
# loss" (the vendor's tools found it, the agent's own synthesis step dropped
# it) shows up as a verdict that got WORSE between the two passes, distinct
# from a case where the vendor simply never found the data at all (both passes
# equally bad). vendor_source_agent_system_prompt_v1.md's own CONSOLIDATION
# section is a direct response to exactly this failure mode.

CONTAINMENT_PROMPT = """You are a quality evaluation judge. Your only task is to compare an answer against an
expected answer (ground truth) and decide whether the answer contains the substance of what
the question asked for. You do not evaluate wording, tone, length or style — content only.

INPUTS you will receive for each case:
- question: the original question text.
- expected_answer: the reference answer (ground truth).
- text_to_score: the text you must score. For the raw-output pass, this is the concatenation
  of ALL of the vendor's tool-call outputs for this question, unsummarized (see RAW OUTPUT
  DEFINITION below). For the final-answer pass, this is the already-consolidated final answer.
  You will be told which one it is.

SCALE (use exactly these three labels, verbatim, including capitalization):
- "Cubierta": all substantive requested elements are present in text_to_score.
- "Parcial": at least one substantive requested element is present, but one or more other
  substantive requested elements are missing or incomplete.
- "No cubierta": none of the substantive requested elements are present, or the returned
  information is of a fundamentally different type from what is expected.
(These three labels are kept in Spanish on purpose — they are the canonical values the
existing scoring pipeline expects. Do not translate them, and do not use any other label.)

CONTAINMENT CRITERION — THE MOST IMPORTANT RULE:
The question you are answering is whether text_to_score CONTAINS the substance of
expected_answer — never the other way around. Do not require exact wording, ordering or
formatting to match. Do not penalize text_to_score for being much longer, more detailed, or
organized differently than expected_answer — that never counts against it. Do not reward
length for its own sake either — a long text that does not contain the requested substance is
still "No cubierta".

ROLE OF QUESTION AND EXPECTED ANSWER:
expected_answer is the primary reference for the substantive content that should be found in
text_to_score. Use question only to interpret the scope and explicit constraints of that
reference — for example, the requested entity, an explicitly specified time period, whether
multiple items are requested, or the type of information being asked for.

Do not independently redefine, expand or improve the expected content based on your own view
of what a better or more complete answer to the question should contain. Do not add
requirements that are not represented in expected_answer.

If question and expected_answer are imperfectly aligned, follow the substantive interpretation
represented by expected_answer unless doing so would violate an explicit and unambiguous
constraint stated in question.

RAW OUTPUT DEFINITION — MULTI-CALL CONCATENATION:
When scoring the raw-output pass, text_to_score is the totality of the vendor's tool-call
outputs for that question, concatenated in full and unsummarized — never a single call
judged in isolation. If the requested substance is present anywhere across that concatenated
set (even if split across several different tool responses), treat it as present for this
pass. This means the raw-output verdict can legitimately be "Cubierta" even when no single
tool call, read alone, would have earned that verdict. Any loss of that information that
happens later — when the agent condenses these raw outputs into its final answer — is not
scored here; it shows up as a lower verdict on the final-answer pass instead (see
CONSOLIDATION LOSS in the usage notes).

EXCEPTION — ANONYMIZED OR CONCRETE VALUES:
Ignore discrepancies in concrete values that may be anonymized or vary for security or
test-data reasons (proper names, IDs, exact figures, specific dates), as long as the TYPE of
data matches. Examples of "same type, different value, do not penalize":
- Both identify an account holder (even if the name doesn't literally match).
- Both give an amount (even if the exact figure differs).
- Both give a date (even if the exact day differs).
- Both cite an applicable regulation, rate or law (even if the exact article number or
  percentage differs).
This includes cases where expected_answer itself may not be up to date (e.g. the ground
truth's figures are older than the vendor's live data). Do not require the exact number to
match. If the question asks for more than one type of figure (e.g. a profit figure and a
loss figure), check that each requested TYPE of figure is present, not that its value
matches expected_answer.
Only lower the verdict when:
(a) an ENTIRE category of requested information is missing (not a different value, but the
    data point is absent altogether),
(b) a different type of data is given than what was asked for (e.g. the question asks for an
    amount and only a date is given),
(c) there is a substantively incorrect claim that contradicts expected_answer or an explicit
    and unambiguous constraint in question — not merely a different concrete value allowed by
    the exceptions above.

EXCEPTION — VAGUE OR UNSPECIFIED TIME PERIOD:
If the question does not pin an exact or concrete time period (it does not name a specific
year, quarter, date range, or "as of" point), do not penalize text_to_score solely for
reporting a different time window than expected_answer — for example, more recent data —
as long as the substance requested is otherwise covered. If the question DOES specify an
exact time period, grade that period strictly: a mismatch on that axis is scored normally
under the containment criterion above (missing or wrong-period data is treated like any
other missing or incorrect element).

EXCEPTION — EPHEMERAL OR POINT-IN-TIME CONTENT:
Some requested content is inherently time-drifting by nature — live job postings, current
pricing, recent reviews, today's open positions, and similar real-world content that
changes continuously and is not expected to stay fixed. When expected_answer is anchored to
one specific (and by now possibly stale) instance of this kind of content, and the question
itself does not ask for that literal historical snapshot, do not penalize text_to_score for
citing a different but equally valid instance that was current at the time of the answer.
Judge whether the right KIND of content is present (e.g. a live job posting from the
relevant company, a recent review, current pricing), not whether it is the exact same
instance recorded in expected_answer.

OUT-OF-SCOPE QUESTIONS AND CLARIFYING RESPONSES:
If text_to_score is an explanation of why the tool cannot answer that question, or a
clarifying question back to the user, score it exactly like any other text: does it contain
the substance of expected_answer? The answer is almost always no, so the verdict will be
"No cubierta" — but this is NOT a judgment on whether that was the right call for the agent to
make (that is not your job), only on whether the requested content is present.

INDEPENDENCE OF THE TWO PASSES:
If you are asked to score both the raw tool output and the final answer for the same
question, score each one independently, without letting one influence the other. It is valid
and expected for the raw tool output to score differently from the final answer on the same
question.

OUTPUT FORMAT — respond with ONLY this JSON, no additional text:
{{
  "verdict": "Cubierta" | "Parcial" | "No cubierta",
  "justification": "1-2 sentences citing which specific element of expected_answer is or is
    not present, and why any exception (anonymized values, time period, ephemeral content)
    does or does not apply.",
  "elements_requested": ["short list of the substantive elements represented in expected_answer, interpreted in the context of question"],
  "elements_found": ["from that list, which ones appear in text_to_score"],
  "elements_missing": ["from that list, which ones do not appear"]
}}

CALIBRATION — worked examples of how to apply the criterion:
1. The question asks for "account holder, amount and date of the transaction". text_to_score
   gives a different (anonymized) holder, a different (anonymized) amount, but DOES give the
   correct exact date → "Cubierta" (all three data types are present, the concrete values
   don't matter).
2. Same question, but text_to_score does not mention the date at all → "Parcial" (an entire
   category out of the three requested is missing).
3. The question asks "who is the ultimate beneficial owner of company X" and text_to_score
   says "I don't have enough information to answer — can you tell me the country of
   incorporation?" → "No cubierta" (the requested substance — the beneficial owner's
   identity — is not present).
4. The question asks for a list of connected companies and text_to_score gives 4 of the 6
   that appear in expected_answer, without indicating the list is partial → "Parcial".
5. text_to_score is three times longer than expected_answer, with extra context that wasn't
   asked for, but it fully includes what the question asked for → "Cubierta" (the extra
   length is not penalized).
6. The question asks "what is the company's current registered address" with no date or
   period named. expected_answer gives an address as of 2022; text_to_score gives a
   different, more recent address with no 2022 address mentioned at all → "Cubierta" (no
   exact period was requested, so a different — and more current — time window is not
   penalized; the substance, a registered address, is present).
7. The question asks "what were the company's Q1 2023 revenue and net loss" (an exact period
   is named). text_to_score gives revenue and net loss figures for Q3 2024 only, with no
   Q1 2023 data → "No cubierta" (the question pins a concrete period, so a different period
   does not satisfy it — this is not covered by the vague-period exception).
8. The question asks for "the company's total revenue and its reported net loss for the most
   recent period available". Across three tool calls, the first call returns only the
   revenue figure, the second call (a different search) returns only the net loss figure, and
   the third is unrelated. Scored on the concatenation of all three raw outputs → "Cubierta"
   (both figures are present somewhere in the totality of tool outputs). The agent's final
   answer, however, only restates the revenue figure and omits the net loss → final-answer
   pass scores "Parcial", producing a consolidation-loss case attributable to the
   consolidation step, not to the underlying tool results.
9. The question asks "show me a current open position at company X" with no reference to a
   specific posting ID or date. expected_answer cites a specific job listing that has since
   been taken down. text_to_score cites a different job listing at the same company, live at
   the time of the answer, with a plausible role and location → "Cubierta" (the ephemeral-
   content exception applies: the question did not ask for that literal listing, only for a
   current opening).
10. The question asks, openly, "what are the company's main risk factors?" (no categories
    named). expected_answer covers three specific categories: regulatory risk, credit risk
    and reputational risk. text_to_score gives a substantive, well-argued discussion of
    market risk and operational risk instead, never touching regulatory, credit or
    reputational risk → "No cubierta" (per ROLE OF QUESTION AND EXPECTED ANSWER: text_to_score
    is a reasonable answer to the open question in the abstract, but the judge must check it
    against the categories actually represented in expected_answer, not against its own idea
    of what a good risk-factors answer would include; none of the three requested categories
    are present).
    Contrast: question asks "excluding subsidiaries, which countries does company X operate
    directly in?" (an explicit and unambiguous constraint: exclude subsidiaries).
    expected_answer lists five countries, two of which are in fact subsidiary-only
    operations. text_to_score lists only the three genuinely-direct countries, correctly
    omitting the two subsidiary-only ones → "Cubierta" (the explicit constraint stated in
    question governs the reading of expected_answer here; omitting elements that the
    question explicitly excludes is not a missing element).
11. The question asks "what is company X's registered business activity code (CNAE)?".
    expected_answer gives only the code, e.g. "6201 - Programming activities". text_to_score
    gives the correct code AND adds, in passing, an unrelated and factually wrong aside (e.g.
    a founding year that does not match reality), on a detail expected_answer says nothing
    about and question never asked for → "Cubierta" (per the narrowed exception (c): only a
    claim that contradicts expected_answer or an explicit constraint in question lowers the
    verdict; a false but tangential claim outside what was requested and outside what
    expected_answer covers is not evaluated here — this judge scores containment of the
    requested substance, not general fact-checking of everything text_to_score says).

Now grade this case:

Question: {question}

Expected answer (ground truth):
{expected_answer}

This is the {pass_label}. text_to_score:
{text_to_score}
"""

_CONTAINMENT_RANK = {"No cubierta": 0, "Parcial": 1, "Cubierta": 2}


@dataclass
class ContainmentResult:
    verdict: str = "ERROR"
    justification: str = ""
    elements_requested: list[str] = field(default_factory=list)
    elements_found: list[str] = field(default_factory=list)
    elements_missing: list[str] = field(default_factory=list)


@dataclass
class ConsolidationResult:
    raw: ContainmentResult
    final: ContainmentResult
    # True when the raw tool output's verdict ranks strictly better than the
    # final answer's -- the vendor found it, the agent's own synthesis lost
    # it. False (not None) when there was nothing to lose (raw was already
    # "No cubierta") or nothing lost (final >= raw).
    consolidation_loss: bool


# Per-tool-call cap on format_raw_tool_output's output -- confirmed live
# 2026-09-17 that Sayari's tool outputs (search_entities running in
# response_mode "export", already known to ignore the requested `limit` --
# see agent/README.md) routinely produce raw_tool_output past 500K-1.2M
# characters when joined uncapped, which silently failed the whole
# judge_consolidation() call (Bedrock rejecting the oversized prompt) for
# 13 of the 66 graded rows in a single run -- 12 of them Sayari, exactly the
# vendor known for this. Capping per call, not just the total, means one
# huge call doesn't crowd out a second call's contribution.
_MAX_CHARS_PER_TOOL_CALL = 15_000


def format_raw_tool_output(tool_calls: list[dict]) -> str:
    """Builds the "raw tool output" text_to_score for judge_containment's
    first pass out of every tool call this turn made, in call order -- not
    just the last one, since a consolidation loss can happen across several
    calls (vendor_source_agent_system_prompt_v1.md's CONSOLIDATION rule 3:
    "consolidate across all calls, not just the last one"). Expects the same
    tool-call dict shape run_vendor_audit.py's .jsonl and a2k.ask's
    `toolCalls[]` both already use (a `toolName`/`output` pair at minimum).
    Each call's own output is truncated to _MAX_CHARS_PER_TOOL_CALL -- see
    that constant's comment for why this exists at all."""
    if not tool_calls:
        return "(no tool calls recorded for this turn)"
    parts = []
    for i, tc in enumerate(tool_calls, 1):
        name = tc.get("toolName") or tc.get("tool_name") or "unknown_tool"
        output = str(tc.get("output"))
        if len(output) > _MAX_CHARS_PER_TOOL_CALL:
            omitted = len(output) - _MAX_CHARS_PER_TOOL_CALL
            output = f"{output[:_MAX_CHARS_PER_TOOL_CALL]}\n... [truncated, {omitted} more characters omitted]"
        parts.append(f"--- Tool call {i}: {name} ---\n{output}")
    return "\n\n".join(parts)


def consolidation_error(reason: str) -> ConsolidationResult:
    """For when judge_consolidation() itself raised -- callers must not just
    swallow that into a bare None (that's how 13/66 rows silently lost their
    containment result with zero trace in a real run before this existed).
    Surfaces the failure the same way pending_manual_review() surfaces a
    no-ground-truth row: as a real, visible result instead of an absence."""
    err = ContainmentResult(verdict="ERROR", justification=reason)
    return ConsolidationResult(raw=err, final=err, consolidation_loss=False)


def judge_containment(bedrock_client, model_id: str, question: str, expected_answer: str, text_to_score: str, *, pass_label: str) -> ContainmentResult:
    """One containment pass -- `pass_label` is folded into the prompt only to
    tell the model which of the two passes this is (e.g. "raw tool output
    from the vendor's tools" or "final, consolidated answer"); it does not
    change the scoring criterion itself, which stays identical between
    passes (see INDEPENDENCE OF THE TWO PASSES in the prompt)."""
    prompt = CONTAINMENT_PROMPT.format(question=question, expected_answer=expected_answer, text_to_score=text_to_score, pass_label=pass_label)
    response = bedrock_client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        # No `temperature`, and 1000 -> 5000 -- see judge()'s own
        # inferenceConfig comment (same reasoningContent-eats-the-budget
        # issue, confirmed live on this pass too).
        inferenceConfig={"maxTokens": 5000},
    )
    text = _extract_text(response)
    parsed = _parse_json_response(text)
    return ContainmentResult(
        verdict=parsed["verdict"],
        justification=parsed.get("justification") or "",
        elements_requested=parsed.get("elements_requested") or [],
        elements_found=parsed.get("elements_found") or [],
        elements_missing=parsed.get("elements_missing") or [],
    )


def judge_consolidation(bedrock_client, model_id: str, question: str, expected_answer: str, raw_tool_output: str, final_answer: str) -> ConsolidationResult:
    """Runs both containment passes and derives consolidation_loss -- the
    comparison itself is done here in Python (rank lookup), not asked of the
    model, same reasoning as judge()'s own weighted_score/verdict: don't ask
    an LLM to do arithmetic/comparison a few lines of code can do exactly."""
    raw = judge_containment(bedrock_client, model_id, question, expected_answer, raw_tool_output, pass_label="raw tool output from the vendor's own tools, before any consolidation by the agent")
    final = judge_containment(bedrock_client, model_id, question, expected_answer, final_answer, pass_label="final, already-consolidated answer the agent produced")
    loss = _CONTAINMENT_RANK.get(raw.verdict, 0) > _CONTAINMENT_RANK.get(final.verdict, 0)
    return ConsolidationResult(raw=raw, final=final, consolidation_loss=loss)
