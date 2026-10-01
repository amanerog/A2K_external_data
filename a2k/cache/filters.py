"""Technical filters T1, T2, T4, T5 (proposal Annex A5): fixed rules, no
model, run before anything that costs money.

T3 was retired in the working session (D17). T6 ("not already rejected by the
user") is not a check here: it is the conditional activation in admission.py,
which only succeeds if the row is still PENDING -- a reformulation that
deprecated it meanwhile makes that update a no-op.

T2 also discards the Vendor Agent's fixed fallback message (E4.9 / D17,
VENDOR_FALLBACK_MESSAGE), by exact match. The proposal's other T2 case, an
answer cut at max_tokens, cannot reach admission: Strands raises
MaxTokensReachedException instead of returning a partial structured answer,
and the agent turns any failed vendor run into that same fallback message
(agent/test_direct_agent_fallback.py pins this).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from .evidence import normalize_for_match, tool_output_text, vendor_tool_calls
from .models import Candidate


@dataclass
class FilterResult:
    passed: bool
    failed: str | None = None  # "T1".."T5"
    reason: str = ""
    verified_sources: list[dict[str, Any]] | None = None


def t1_calls_ok(candidate: Candidate) -> FilterResult:
    calls = vendor_tool_calls(candidate.tool_calls)
    bad = [tc.get("toolName") for tc in calls if tc.get("status") != "success" or tc.get("errorType")]
    if bad:
        return FilterResult(False, "T1", f"tool calls with errors: {', '.join(map(str, bad))}")
    return FilterResult(True)


def t2_answer_complete(candidate: Candidate, *, min_chars: int, fallback_messages: tuple[str, ...] = ()) -> FilterResult:
    answer = (candidate.answer or "").strip()
    if len(answer) < min_chars:
        return FilterResult(False, "T2", f"answer shorter than {min_chars} characters ({len(answer)})")
    if any(answer == msg.strip() for msg in fallback_messages):
        return FilterResult(False, "T2", "answer is the Vendor Agent fallback message")
    return FilterResult(True)


def t4_from_vendor_data(candidate: Candidate) -> FilterResult:
    for tc in vendor_tool_calls(candidate.tool_calls):
        if tool_output_text(tc.get("output")).strip():
            return FilterResult(True)
    return FilterResult(False, "T4", "no tool call returned content: answer is not backed by vendor data")


_URL = re.compile(r"https?://[^\s)\]>\"'`,]+")


def _url_variants(url: str) -> list[str]:
    url = url.rstrip(".;:")
    bare = re.sub(r"^https?://", "", url).rstrip("/")
    return [normalize_for_match(url), normalize_for_match(bare)]


def t5_sources_real(candidate: Candidate) -> FilterResult:
    """Every source the answer cites -- the agent's citations (sourceUrl /
    documentId) plus any URL written into the answer text -- must appear
    literally in some tool output; a source that is not there is treated as
    invented and the answer is not cached. At least one source is required.
    The verified citations are what gets stored as the entry's `sources`."""
    corpus = normalize_for_match(
        "\n".join(tool_output_text(tc.get("output")) for tc in vendor_tool_calls(candidate.tool_calls))
    )

    def found(value: str) -> bool:
        return any(v and v in corpus for v in _url_variants(value))

    verified: list[dict[str, Any]] = []
    invented: list[str] = []
    for citation in candidate.citations:
        refs = [r for r in (citation.get("sourceUrl"), citation.get("documentId")) if r]
        if not refs:
            continue
        if any(found(r) for r in refs):
            verified.append({**citation, "vendor": candidate.vendor})
        else:
            invented.append(refs[0])

    for url in _URL.findall(candidate.answer or ""):
        if not found(url):
            invented.append(url)

    if invented:
        return FilterResult(False, "T5", f"sources not found in tool output: {', '.join(sorted(set(invented))[:5])}")
    if not verified:
        return FilterResult(False, "T5", "answer cites no source")
    return FilterResult(True, verified_sources=verified)


def run(candidate: Candidate, *, min_chars: int, fallback_messages: tuple[str, ...] = (), check_sources: bool = True) -> FilterResult:
    """`check_sources=False` skips T5 -- only for offline runs over answers
    recorded without their citations (the POC .jsonl files)."""
    checks = [
        lambda: t1_calls_ok(candidate),
        lambda: t2_answer_complete(candidate, min_chars=min_chars, fallback_messages=fallback_messages),
        lambda: t4_from_vendor_data(candidate),
    ]
    if check_sources:
        checks.append(lambda: t5_sources_real(candidate))
    for check in checks:
        result = check()
        if not result.passed:
            return result
    return result
