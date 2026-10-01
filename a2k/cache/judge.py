"""Admission step B (proposal Annex A6): the per-domain judge.

It sees the question, the final answer, the raw tool output and what the
classifier prepared, and reports findings -- per requested element, the
common core C1-C4, and the checks of each loaded domain module. It does NOT
decide FIT / NOT FIT: decision.py does, in code, on these findings.

Different from the retrieval Checker (checker.py): the Checker
asks "is this cached answer right for this new question", the judge asks
"is this vendor answer good enough to be cached at all".

Each covered element must come with a verbatim quote from the tool output;
decision.py verifies the quote is really there, so grounding (C3) does not
rest on the model's word alone.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from .bedrock import ModelOutputError, converse_json
from .models import Classification, ElementFinding, JudgeFindings
from .taxonomy import DOMAINS

CORE_VERSION = "judge-core-v5"
MODULES_VERSION = "judge-modules-v3"

CORE_CHECKS = (
    ("C1", "Every requested element of the question is covered by the answer."),
    ("C2", "The data is of the type asked for (a figure, a list, a status, a date...)."),
    ("C3", "Every statement in the answer is grounded in the raw tool output below, not in general knowledge."),
    ("C4", "The subject is the one asked about (the right entity, person, series or period)."),
)

_PROMPT = """You decide whether an answer produced by a data vendor's agent is good enough to be \
stored in a cache and served again to other users who ask the same question. A wrong answer in \
the cache is served many times, so be strict: when in doubt, a check fails.

You have no reference answer. Judge the answer only against the question, the list of requested \
elements, and the raw output of the vendor tools the agent called (the only evidence that counts). \
<answer_sources> lists the sources the agent cited alongside its answer; they are part of the \
answer for any check about where the answer's information comes from.

<question>
{question}
</question>

<requested_elements>
{elements}
</requested_elements>

<answer>
{answer}
</answer>

<answer_sources>
{sources}
</answer_sources>

<raw_tool_output>
{evidence}
</raw_tool_output>

1. For EACH requested element, in the same order, report:
   - status: "covered" (the answer gives it fully and the tool output supports it), "partial" \
(only part of it, or only partly supported), or "missing".
   - An answer that says the information was not found, is not available or could not be \
retrieved does NOT cover that element: "missing". The one exception is a question asking whether \
something exists or applies (sanctions, watchlists, litigation, adverse media): there, "nothing \
was found" is an answer, "covered" only if the tool output shows that searches were actually run \
and returned nothing, and the answer says where it looked.
   - evidence_quote: for "covered" and "partial", a short passage (at most 300 characters) copied \
VERBATIM from <raw_tool_output> that supports the answer for this element. Copy it exactly, \
character for character, without paraphrasing, translating, joining distant fragments or \
shortening it with "..." -- pick a shorter passage instead. null if "missing".

2. Common checks -- for each, "pass" or "fail" with a one-sentence reason:
{core_checks}

3. Domain checks -- for each, "pass", "fail" or "not_applicable" (only when the check clearly \
does not concern what this question asks), with a one-sentence reason:
{module_checks}

4. data_kind and data_as_of -- how the information in the answer is dated:
   - data_kind: "current" if the tool output presents it as the state of things today -- a \
registry record, a company's status or address, a rule or guideline in force, a current board or \
management team -- even if the vendor captured it some time ago. "snapshot" if it is tied to a \
moment or period -- news, media coverage, a report or press release, a price or product launch, a \
figure for a given period.
   - data_as_of (YYYY-MM-DD): the most recent date, in the tool output, as of which the \
information the answer relies on was current -- a capture/retrieval date, a publication date, or \
the end of the period a figure refers to. Not the date of the facts themselves (an incorporation \
date, the date a status last changed, the year a rule was issued). null if there is none.

Respond with ONLY this JSON object:
{{"elements": [{{"element": "...", "status": "covered", "evidence_quote": "..."}}],
"core": {{"C1": {{"result": "pass", "reason": "..."}}, "C2": {{...}}, "C3": {{...}}, "C4": {{...}}}},
"modules": {{"<check id>": {{"result": "pass", "reason": "..."}}}},
"data_kind": "snapshot", "data_as_of": "YYYY-MM-DD"}}
"""


def module_checks(classification: Classification) -> list[tuple[str, str]]:
    checks: list[tuple[str, str]] = []
    for code in classification.domains:
        checks.extend(DOMAINS[code].checks)
    return checks


def _format_sources(citations: list[dict[str, Any]]) -> str:
    lines = []
    for c in citations:
        parts = [str(c[k]) for k in ("title", "sourceUrl", "documentId") if c.get(k)]
        if parts:
            lines.append("- " + " | ".join(parts))
    # Empty only when replaying answers recorded without their citations (offline
    # and e2e runs): in production T5 rejects an answer with no verified source
    # before the judge is called. Read as "no sources" it failed CRP-3 on an answer
    # whose text names the Receita Federal (e2e, id 38, 2026-10-01).
    return "\n".join(lines) or "(not available for this answer -- judge any check about sources on the answer text alone)"


def build_prompt(*, question: str, answer: str, evidence: str, classification: Classification, citations: list[dict[str, Any]] = ()) -> str:
    modules = module_checks(classification)
    return _PROMPT.format(
        question=question,
        sources=_format_sources(list(citations)),
        elements="\n".join(f"{i}. {e}" for i, e in enumerate(classification.requested_elements, start=1)),
        answer=answer,
        evidence=evidence,
        core_checks="\n".join(f"   - {cid}: {text}" for cid, text in CORE_CHECKS),
        module_checks="\n".join(f"   - {cid}: {text}" for cid, text in modules) or "   (none)",
    )


def _check(raw: Any) -> dict[str, str]:
    if isinstance(raw, dict):
        return {"result": str(raw.get("result") or "fail").lower(), "reason": str(raw.get("reason") or "")}
    return {"result": str(raw or "fail").lower(), "reason": ""}


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(str(value)[:10])
    except ValueError:
        return None


def parse_findings(raw: dict[str, Any], classification: Classification) -> JudgeFindings:
    elements = []
    for item in raw.get("elements") or []:
        if not isinstance(item, dict):
            continue
        status = str(item.get("status") or "missing").lower()
        if status not in ("covered", "partial", "missing"):
            status = "missing"
        quote = item.get("evidence_quote")
        elements.append(ElementFinding(element=str(item.get("element") or ""), status=status, evidence_quote=str(quote) if quote else None))
    if len(elements) != len(classification.requested_elements):
        raise ModelOutputError(
            f"judge reported {len(elements)} elements for {len(classification.requested_elements)} requested"
        )
    core_raw = raw.get("core") or {}
    modules_raw = raw.get("modules") or {}
    # A check the model skipped counts as failed, not as passed.
    core = {cid: _check(core_raw.get(cid)) for cid, _ in CORE_CHECKS}
    modules = {cid: _check(modules_raw.get(cid)) for cid, _ in module_checks(classification)}
    # Anything but an explicit "current" counts as dated: the conservative side.
    data_kind = "current" if str(raw.get("data_kind") or "").lower() == "current" else "snapshot"
    return JudgeFindings(
        elements=elements, core=core, modules=modules, data_as_of=_parse_date(raw.get("data_as_of")), data_kind=data_kind, raw=raw
    )


def judge(
    bedrock_client,
    *,
    model_id: str,
    question: str,
    answer: str,
    evidence: str,
    classification: Classification,
    citations: list[dict[str, Any]] = (),
) -> JudgeFindings:
    raw = converse_json(
        bedrock_client,
        model_id=model_id,
        prompt=build_prompt(question=question, answer=answer, evidence=evidence, classification=classification, citations=citations),
        # 8000 truncated one POCv3 verdict (id 81): reasoning + a long findings list.
        max_tokens=16000,
    )
    return parse_findings(raw, classification)
