"""Retrieval step 1 (proposal Sheets 4 and 5): is this question a
reformulation of the previous turn of the same session?

Only checked when the previous turn is within the window (D5, 10 minutes);
outside it no model is called. If it is a reformulation, the entry served on
the previous turn -- a cache hit, or the admission candidate made from the
vendor answer -- is deprecated (status_reason immediate_reformulation) before
the new question is searched, so the user never gets the same answer again
and a still-PENDING candidate is dropped by admission (T6). It also feeds the
re-ask rate, the go / no-go metric of D14.

Needs a session id: the caller's (a2k.ask `sessionId`, sent by K2), or else
one derived from the MCP/AgentCore session of the connection
(mcp_server/server.py _session_id). Without either, nothing is checked.
"""

from __future__ import annotations

from typing import Any

from .bedrock import converse_json
from .store import CacheStore

PROMPT_VERSION = "reformulation-v1"
DEPRECATION_REASON = "immediate_reformulation"
KINDS = ("reformulation", "follow_up", "new")

_PROMPT = """Two consecutive questions were asked, a few minutes apart, by the same user of an \\
external-data service for a bank. Decide how the second relates to the first.

<first_question>
{previous}
</first_question>

<second_question>
{current}
</second_question>

- "reformulation": the second asks again for the SAME information as the first -- in other \\
words, more precisely, with a correction (another spelling of a name, an identifier, a narrower \\
period) or simply repeated. This is what a user does when the first answer did not give them \\
what they wanted.
- "follow_up": the second builds on the first answer -- more detail, another aspect of the same \\
subject, a related entity, a comparison.
- "new": the second is about something else.

The questions may be in different languages. Respond with ONLY this JSON object:
{{"kind": "reformulation" | "follow_up" | "new", "reason": "one short sentence"}}
"""


def classify(bedrock_client, *, model_id: str, previous: str, current: str) -> tuple[str, str]:
    raw = converse_json(bedrock_client, model_id=model_id, prompt=_PROMPT.format(previous=previous, current=current), max_tokens=1000)
    kind = str(raw.get("kind") or "").lower().replace("-", "_")
    # Anything unrecognised is treated as unrelated: never deprecate on a guess.
    return (kind if kind in KINDS else "new"), str(raw.get("reason") or "")


def check(
    question: str,
    session_id: str,
    *,
    bedrock_client,
    store: CacheStore,
    model_id: str,
    window_seconds: float,
) -> dict[str, Any]:
    """Never raises; what it returns goes into the serving log."""
    out: dict[str, Any] = {"previous_turn": False}
    try:
        turn = store.last_turn(session_id, window_seconds=window_seconds)
        if not turn:
            return out
        out.update(previous_turn=True, previous_turn_age_s=round(turn["age_seconds"], 1))
        out["kind"], out["reason"] = classify(bedrock_client, model_id=model_id, previous=turn["last_query_text"], current=question)
        if out["kind"] == "reformulation" and turn["served_entry_id"]:
            out["previous_entry_id"] = turn["served_entry_id"]
            out["deprecated"] = store.deprecate(turn["served_entry_id"], DEPRECATION_REASON)
    except Exception as exc:  # noqa: BLE001 -- the cache must never break a request
        out["error"] = f"{type(exc).__name__}: {exc}"
    return out
