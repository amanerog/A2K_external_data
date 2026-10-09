"""The evidence bundle (raw tool output behind an answer) and the plain text
the judge reads and the filters/decision rule check against.

Vendor tool outputs arrive as MCP results whose `content[].text` is itself
often a JSON document serialized into a string. Matching a URL or a quoted
sentence against `json.dumps(output)` would miss anything with non-ASCII
characters or escaped quotes, so the text is built from the string leaves of
the output, decoding nested JSON strings along the way.
"""

from __future__ import annotations

import json
import re
from typing import Any

from .models import Candidate

# Tools that are not vendor data: the agent's structured-output tool, and the
# AgentSkills `skills` loader.
NON_VENDOR_TOOLS = {"AskContent", "SearchContent", "skills"}


def vendor_tool_calls(tool_calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [tc for tc in tool_calls if tc.get("toolName") not in NON_VENDOR_TOOLS]


def _string_leaves(value: Any, out: list[str], depth: int = 0) -> None:
    if depth > 12:
        return
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "[") and len(stripped) > 1:
            try:
                _string_leaves(json.loads(stripped), out, depth + 1)
                return
            except json.JSONDecodeError:
                pass
        out.append(value)
    elif isinstance(value, dict):
        for v in value.values():
            _string_leaves(v, out, depth + 1)
    elif isinstance(value, list):
        for v in value:
            _string_leaves(v, out, depth + 1)
    elif value is not None:
        out.append(str(value))


def tool_output_text(output: Any) -> str:
    leaves: list[str] = []
    _string_leaves(output, leaves)
    return "\n".join(leaves)


def evidence_text(candidate: Candidate) -> str:
    """What the judge reads as "raw tool output": one section per vendor tool
    call, with the arguments it was called with."""
    sections = []
    for i, tc in enumerate(vendor_tool_calls(candidate.tool_calls), start=1):
        args = json.dumps(tc.get("input") or {}, ensure_ascii=False)
        sections.append(f"### Tool call {i}: {tc.get('vendor')}/{tc.get('toolName')} {args}\n{tool_output_text(tc.get('output'))}")
    return "\n\n".join(sections)


_WS = re.compile(r"\s+")


def normalize_for_match(text: str) -> str:
    return _WS.sub(" ", text).strip().lower()


def bundle(candidate: Candidate) -> dict[str, Any]:
    """S3 evidence bundle, shape from the proposal's Annex A2."""
    return {
        "entry_id": candidate.entry_id,
        "nl_query": candidate.question,
        "final_answer": candidate.answer,
        "chosen_vendor": candidate.vendor,
        "citations": candidate.citations,
        "created_at": candidate.created_at.isoformat(),
        "tool_calls": [
            {
                "vendor": tc.get("vendor"),
                "tool": tc.get("toolName"),
                "args": tc.get("input"),
                "status": tc.get("status"),
                "error_type": tc.get("errorType"),
                "response": tc.get("output"),
            }
            for tc in vendor_tool_calls(candidate.tool_calls)
        ],
    }


def evidence_key(candidate: Candidate) -> str:
    return f"cache-evidence/{candidate.created_at:%Y/%m}/{candidate.entry_id}.json"


# --- Evidence cut to the cited pages (measured offline before any use) -----

_URL = re.compile(r"https?://[^\s\"'<>)\]]+")


def normalize_url(url: str) -> str:
    return url.strip().split("#", 1)[0].rstrip("/").lower()


def _decoded(value: Any, depth: int = 0) -> Any:
    """The output with every nested JSON string parsed into structure."""
    if depth > 12:
        return value
    if isinstance(value, str):
        stripped = value.strip()
        if stripped[:1] in ("{", "[") and len(stripped) > 1:
            try:
                return _decoded(json.loads(stripped), depth + 1)
            except json.JSONDecodeError:
                return value
        return value
    if isinstance(value, dict):
        return {k: _decoded(v, depth + 1) for k, v in value.items()}
    if isinstance(value, list):
        return [_decoded(v, depth + 1) for v in value]
    return value


def _urls(value: Any) -> set[str]:
    leaves: list[str] = []
    _string_leaves(value, leaves)
    return {normalize_url(u) for leaf in leaves for u in _URL.findall(leaf)}


def _trim(value: Any, cited: set[str]) -> Any:
    """Drops every list item that carries URLs, none of them cited."""
    if isinstance(value, dict):
        return {k: _trim(v, cited) for k, v in value.items()}
    if isinstance(value, list):
        kept = []
        for item in value:
            urls = _urls(item) if isinstance(item, (dict, list)) else set()
            if urls and not urls & cited:
                continue
            kept.append(_trim(item, cited))
        return kept
    return value


def cited_evidence_text(candidate: Candidate) -> tuple[str, dict[str, Any]]:
    """evidence_text cut to the pages the answer cites: a call that fetched
    given pages stays only if one of them is cited; in any other output, a
    list item (a search result, a Cala context excerpt) that carries URLs,
    none cited, is dropped; data with no URL (entity records) stays whole.
    Falls back to the full text when the answer cites nothing, or nothing it
    cites is found in the output. Returns (text, how it was cut)."""
    cited = {normalize_url(c["sourceUrl"]) for c in candidate.citations if c.get("sourceUrl")}
    if not cited:
        return evidence_text(candidate), {"mode": "full", "reason": "the answer cites no URL"}
    sections, matched = [], set()
    for i, tc in enumerate(vendor_tool_calls(candidate.tool_calls), start=1):
        args = json.dumps(tc.get("input") or {}, ensure_ascii=False)
        fetched = _urls(tc.get("input"))
        if fetched:
            if fetched & cited:
                matched |= fetched & cited
                text = tool_output_text(tc.get("output"))
            else:
                text = "(page not cited by the answer: omitted)"
        else:
            output = _trim(_decoded(tc.get("output")), cited)
            matched |= _urls(output) & cited
            text = tool_output_text(output)
        sections.append(f"### Tool call {i}: {tc.get('vendor')}/{tc.get('toolName')} {args}\n{text}")
    if not matched:
        return evidence_text(candidate), {"mode": "full", "reason": "no cited URL found in the tool output"}
    return "\n\n".join(sections), {"mode": "cited", "cited": len(cited), "found": len(matched)}


def cap_sections(text: str, max_chars: int) -> str:
    """Each tool call's section of an evidence text cut to `max_chars`."""
    sections = text.split("\n\n### Tool call ")
    return "\n\n### Tool call ".join(s if len(s) <= max_chars else s[:max_chars] + "\n[... cut]" for s in sections)
