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

import re
from typing import Any

from .bedrock import converse_json
from .store import CacheStore

# v2: the same subject is required, and the line continuations are real (v1
# sent the model a literal backslash at the end of five lines).
# v3: refers_back -- the words of the second question that point back to the
# first one's subject, checked in code when only one question names an identifier.
PROMPT_VERSION = "reformulation-v3"
DEPRECATION_REASON = "immediate_reformulation"
KINDS = ("reformulation", "follow_up", "new")

_PROMPT = """Two consecutive questions were asked, a few minutes apart, by the same user of an \
external-data service for a bank. Decide how the second relates to the first.

<first_question>
{previous}
</first_question>

<second_question>
{current}
</second_question>

- "reformulation": the second asks again for the SAME information about the SAME subject (the \
same company, person, document or series) -- in other words, more precisely, with a correction \
(another spelling of the same name, the identifier of that same subject, a narrower period) or \
simply repeated. This is what a user does when the first answer did not give them what they \
wanted.
- "follow_up": the second builds on the first answer -- more detail, another aspect of the same \
subject, a related entity, a comparison.
- "new": the second is about something else.

The same kind of question about ANOTHER subject is "new", never "reformulation", however similar \
the wording: "what is registered for company A?" followed by "what is registered for company B?", \
or the same question with a different tax ID, registry number or LEI. If you cannot tell whether \
the two questions name the same subject, answer "new".

refers_back: the exact words of the second question, copied as written, that point back to the subject of the first one ("its tax ID", "that company", "the same entity", "su CIF", "esa empresa", "dessa empresa"), or "" if it has none. A name or an identifier on its own is not a reference back.

The questions may be in different languages. Respond with ONLY this JSON object:
{{"kind": "reformulation" | "follow_up" | "new", "refers_back": "...", "reason": "one short sentence"}}
"""

# Identifier-like tokens: tax IDs, registry numbers, LEIs, series codes --
# at least 5 digits, letters and separators allowed (P3701000F, 18.236.120/0001-58).
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9./-]*\d[A-Za-z0-9./-]*")
# Dates are periods, not subjects: a narrower period is a reformulation.
_DATE = re.compile(r"\d{4}[-/.]\d{1,2}([-/.]\d{1,2})?|\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}")


def identifiers(text: str) -> set[str]:
    found = set()
    for token in _TOKEN.findall(text):
        if _DATE.fullmatch(token.rstrip(".")):
            continue
        normalized = re.sub(r"[^0-9A-Za-z]", "", token).upper()
        if sum(ch.isdigit() for ch in normalized) >= 5:
            found.add(normalized)
    return found


def classify(bedrock_client, *, model_id: str, previous: str, current: str) -> tuple[str, str]:
    # Guard 1, before any model call: two questions that each name an
    # identifier, with none in common, are about different subjects. The model
    # read "tax code P3701000F" then "tax code B43995760" as a correction and
    # deprecated a good entry (full shadow run, 2026-10-06).
    before, after = identifiers(previous), identifiers(current)
    if before and after and not (before & after):
        return "new", "different identifiers: not the same subject"
    raw = converse_json(bedrock_client, model_id=model_id, prompt=_PROMPT.format(previous=previous, current=current), max_tokens=1000)
    kind = str(raw.get("kind") or "").lower().replace("-", "_")
    # Anything unrecognised is treated as unrelated: never deprecate on a guess.
    kind = kind if kind in KINDS else "new"
    reason = str(raw.get("reason") or "")
    # Guard 2: when only one question names an identifier (a company by its
    # name, then a tax code), nothing in the texts says the identifier is that
    # company's -- the model assumed it for unrelated pairs (validation run,
    # 2026-10-06: "ENJOY PARKING SL." then "P3701000F", which the day before
    # it had paired with another company). A reformulation then needs the
    # second question to point back to the first, in words found in it.
    if kind == "reformulation" and bool(before) != bool(after):
        refers_back = str(raw.get("refers_back") or "").strip()
        squash = lambda text: " ".join(text.lower().split())  # noqa: E731
        if not refers_back or squash(refers_back) not in squash(current):
            return "new", "only one question names an identifier and the second does not refer back to the first"
    return kind, reason


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
