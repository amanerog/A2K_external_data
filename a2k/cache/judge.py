"""Admission step B (proposal Annex A6): the per-domain judge.

It sees the question, the final answer, the raw tool output and what the
classifier prepared, and reports findings -- per requested element, the
common core C1-C4, and the checks of each loaded domain module. It does NOT
decide FIT / NOT FIT: decision.py does, in code, on these findings.

Different from the retrieval Checker (checker.py): the Checker
asks "is this cached answer right for this new question", the judge asks
"is this vendor answer good enough to be cached at all".

System prompt = prompts/juez_admision_v1.md with {{DOMAIN_MODULES}} replaced
by the module of each domain the classifier returned, plus the ADAPTATIONS
below; the inputs go in the user message, each in its tag.
"""

from __future__ import annotations

import json
from datetime import date
from typing import Any

from . import prompts
from .bedrock import ModelOutputError, converse_json
from .models import Classification, ElementFinding, JudgeFindings
from .taxonomy import DOMAINS

CORE_PROMPT = "juez_admision_v1"
# Bump when ADAPTATIONS change; the module files carry their own version.
CORE_VERSION = f"{CORE_PROMPT}+a2k-1"
MODULES_VERSION = "modulos_admision_v1"

CORE_CHECKS = ("C1", "C2", "C3", "C4")

# What the code needs and the delivered core does not have, each one a
# replacement that must match the core exactly once (checked at import, so a
# new version of the file that moves the text fails loudly, not silently):
# 1. <answer_sources>: without the citations, an answer naming the Receita
#    Federal failed CRP-3 (e2e, POCv3 id 38, 2026-10-01).
# 2. evidence_quote: a verbatim passage of the raw tool output per element,
#    which decision.py looks up -- C3 does not rest on the model's word alone.
# 3. data_kind + the most recent date instead of the OLDEST: dating Cala's
#    answers by their oldest fact left most of them expired on arrival; the
#    mixed rule was decided in the working session of 2026-10-01 (expiry.py).
ADAPTATIONS: tuple[tuple[str, str], ...] = (
    (
        "- <classification>: JSON from the admission classifier",
        "- <answer_sources>: the sources the agent cited alongside its final answer. They are part\n"
        "  of the answer for any check about where its information comes from. If it says \"not\n"
        "  available\", judge those checks on the answer text alone.\n"
        "- <classification>: JSON from the admission classifier",
    ),
    (
        "true / false, or null if the element is missing.\n",
        "true / false, or null if the element is missing.\n"
        "For each covered or partial element, also give evidence_quote: a short passage (at most 300\n"
        "characters) copied VERBATIM from <raw_tool_output> that supports the answer for that\n"
        "element. Copy it exactly, character for character, without paraphrasing, translating,\n"
        "joining distant fragments or shortening it with \"...\" -- pick a shorter passage instead.\n"
        "null if the element is missing. It is checked automatically against the raw tool output.\n",
    ),
    (
        "     \"evidence\": \"short quote or paraphrase of where the answer gives it, or ''\"}",
        "     \"evidence\": \"short quote or paraphrase of where the answer gives it, or ''\",\n"
        "     \"evidence_quote\": \"verbatim passage of raw_tool_output\" | null}",
    ),
    (
        "data_as_of: the date the information in the answer refers to, taken from the raw tool\n"
        "output: a publication date, an \"as of\" date, a registry extraction date, the end of the\n"
        "reported period, the date of the version in force. If several facts carry different dates,\n"
        "use the OLDEST date among those that answer the requested elements. Return null if the raw\n"
        "tool output gives no date for them; do not use today's date and do not guess.",
        "data_kind: \"current\" if the raw tool output presents the information as the state of\n"
        "things today -- a registry record, a company's status or address, a rule or guideline in\n"
        "force, a current board or management team -- even if the vendor captured it some time ago.\n"
        "\"snapshot\" if it is tied to a moment or period -- news, media coverage, a report or press\n"
        "release, a price or product launch, a figure for a given period.\n"
        "data_as_of: the most recent date, in the raw tool output, as of which the information the\n"
        "answer relies on was current -- a capture or extraction date, a publication date, an \"as\n"
        "of\" date, or the end of the period a figure refers to. Not the date of the facts\n"
        "themselves (an incorporation date, the date a status last changed, the year a rule was\n"
        "issued). Return null if the raw tool output gives no such date; do not use today's date\n"
        "and do not guess.",
    ),
    (
        "  \"data_as_of\": \"YYYY-MM-DD\" | null,",
        "  \"data_kind\": \"current\" | \"snapshot\",\n  \"data_as_of\": \"YYYY-MM-DD\" | null,",
    ),
)


def _adapted_core() -> str:
    text = prompts.block(CORE_PROMPT)
    for old, new in ADAPTATIONS:
        if text.count(old) != 1:
            raise RuntimeError(f"{CORE_PROMPT}.md: adaptation anchor found {text.count(old)} times: {old[:60]!r}")
        text = text.replace(old, new)
    return text


_CORE = _adapted_core()


def module_checks(classification: Classification) -> list[str]:
    return [cid for code in classification.domains for cid in DOMAINS[code].checks]


def system_prompt(classification: Classification) -> str:
    modules = "\n\n".join(DOMAINS[code].module_text for code in classification.domains)
    return _CORE.replace("{{DOMAIN_MODULES}}", modules)


def _format_sources(citations: list[dict[str, Any]]) -> str:
    lines = []
    for c in citations:
        parts = [str(c[k]) for k in ("title", "sourceUrl", "documentId") if c.get(k)]
        if parts:
            lines.append("- " + " | ".join(parts))
    # Empty only when replaying answers recorded without their citations (offline
    # and e2e runs): in production T5 rejects an answer with no verified source
    # before the judge is called.
    return "\n".join(lines) or "(not available for this answer)"


def user_message(
    *, question: str, answer: str, evidence: str, classification: Classification, citations: list[dict[str, Any]], today: date
) -> str:
    return (
        f"<question>\n{question}\n</question>\n\n"
        f"<final_answer>\n{answer}\n</final_answer>\n\n"
        f"<answer_sources>\n{_format_sources(citations)}\n</answer_sources>\n\n"
        f"<raw_tool_output>\n{evidence}\n</raw_tool_output>\n\n"
        f"<classification>\n{json.dumps(classification.raw, ensure_ascii=False, indent=1)}\n</classification>\n\n"
        f"<today>{today.isoformat()}</today>"
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


def _modules(raw: Any) -> dict[str, Any]:
    # The core asks for a list of {"domain", "check", "result", "reason"}.
    if isinstance(raw, list):
        return {str(m.get("check")): m for m in raw if isinstance(m, dict) and m.get("check")}
    return raw if isinstance(raw, dict) else {}


def parse_findings(raw: dict[str, Any], classification: Classification) -> JudgeFindings:
    reported = [item for item in raw.get("elements") or [] if isinstance(item, dict)]
    if len(reported) != len(classification.requested_elements):
        raise ModelOutputError(
            f"judge reported {len(reported)} elements for {len(classification.requested_elements)} requested"
        )
    by_id = {str(item.get("id")): item for item in reported if item.get("id")}
    elements = []
    for position, requested in enumerate(classification.requested_elements):
        item = by_id.get(requested.id) or reported[position]
        status = str(item.get("coverage") or item.get("status") or "missing").lower()
        if status not in ("covered", "partial", "missing"):
            status = "missing"
        quote = item.get("evidence_quote")
        data_type_ok = item.get("data_type_ok")
        elements.append(
            ElementFinding(
                id=requested.id,
                element=requested.element,
                status=status,
                evidence_quote=str(quote) if quote else None,
                data_type_ok=data_type_ok if isinstance(data_type_ok, bool) else None,
                evidence=str(item.get("evidence") or ""),
            )
        )
    core_raw = raw.get("core") or {}
    modules_raw = _modules(raw.get("modules"))
    # A check the model skipped counts as failed, not as passed.
    core = {cid: _check(core_raw.get(cid)) for cid in CORE_CHECKS}
    modules = {cid: _check(modules_raw.get(cid)) for cid in module_checks(classification)}
    # Anything but an explicit "current" counts as dated: the conservative side.
    data_kind = "current" if str(raw.get("data_kind") or "").lower() == "current" else "snapshot"
    return JudgeFindings(
        elements=elements,
        core=core,
        modules=modules,
        data_as_of=_parse_date(raw.get("data_as_of")),
        data_kind=data_kind,
        unsupported_claims=[str(c) for c in raw.get("unsupported_claims") or []],
        justification=str(raw.get("justification") or ""),
        raw=raw,
    )


def judge(
    bedrock_client,
    *,
    model_id: str,
    question: str,
    answer: str,
    evidence: str,
    classification: Classification,
    today: date,
    citations: list[dict[str, Any]] = (),
) -> JudgeFindings:
    raw = converse_json(
        bedrock_client,
        model_id=model_id,
        system=system_prompt(classification),
        prompt=user_message(
            question=question, answer=answer, evidence=evidence, classification=classification, citations=list(citations), today=today
        ),
        # 8000 truncated one POCv3 verdict (id 81): reasoning + a long findings list.
        max_tokens=16000,
    )
    return parse_findings(raw, classification)
