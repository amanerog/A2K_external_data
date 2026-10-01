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
