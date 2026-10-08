"""The retrieval Checker (proposal Sheet 4, step 5): two independent model
calls on each candidate the vector search returns, both required before an
entry counts as a hit.

  filter 1 -- are the two questions asking for the same information?
  filter 2 -- does the cached answer actually answer the new question?

D4 (working session 2026-09-30): the Checker stays exactly as tested --
94.8-100% on the false-positive / false-negative sets -- so these prompts and
the model are the ones from the original DynamoDB-backed cache (the removed
a2k/gateway/cache.py); only the inputs change: English texts on both sides.
"""

from __future__ import annotations

from datetime import date

from .bedrock import parse_json_object

# v2: the only change to the prompts as tested (D4) is the dates block: the
# Checker turned down exact repeats of "this week" / "the past quarter"
# questions because it could not tell the saved answer was from the same week
# (full shadow run, 2026-10-06).
# v3: v2 read every "current", "latest" or "recently" as tied to the day it
# was asked, and turned down good hits only because two days had passed
# (4c test, 2026-10-08) -- the saved answer is still within its validity, set
# by the cache's own expiry. Only a named period that falls elsewhere counts.
PROMPT_VERSION = "checker-v3-dates"

_DATES = """Today is {today}. The previously asked question was asked, and its answer produced, on \
{cached_on}. The saved answer is still within its validity period, so "current", "latest", \
"recent" or a question with no date at all do NOT make the two questions different just because \
they were asked on different days. Treat the dates as different only for a named calendar period \
that falls elsewhere on the two dates: "this week" asked in another calendar week, "this month" \
in another month, "the past quarter" in another quarter.

"""

EQUIVALENCE_PROMPT = """Decide whether these two questions are asking for the same underlying \
information -- not just topically similar, but such that a correct answer to one would also be \
a correct, complete answer to the other.

{dates}Previously asked question: {cached_query}

New question: {new_query}

Answer "no" if the new question asks about a different entity, a different direction of a \
relationship (e.g. "who controls X" vs "what does X control"), a different time period, or any \
other distinct piece of information -- even if the wording is very similar. Answer "yes" only if \
someone asking either question would expect the exact same answer.

Respond with ONLY this JSON, no other text:
{{"equivalent": true or false, "reason": "one short sentence"}}
"""

SATISFIES_PROMPT = """A caller previously asked a question and received an answer, which was \
saved for reuse. A new question just came in that an embedding search flagged as a possible \
match for that saved answer. Decide whether the saved answer actually, substantively answers \
the new question -- not just whether the two questions are topically similar.

{dates}Previously asked question: {cached_query}

Saved answer:
{cached_answer}

New question: {new_query}

Answer "no" if the new question asks for something different (a different entity, a different \
direction of a relationship, a different fact than what the saved answer addresses), even if \
the wording is very similar to the previous question. Answer "yes" only if the saved answer \
would genuinely satisfy someone asking the new question right now.

Respond with ONLY this JSON, no other text:
{{"satisfies": true or false, "reason": "one short sentence"}}
"""


def _converse(bedrock_client, model_id: str, prompt: str) -> dict:
    # temperature=0 as tested; the Checker model (Sonnet 4.5) accepts it.
    response = bedrock_client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 200, "temperature": 0},
    )
    for block in response["output"]["message"]["content"]:
        if "text" in block:
            return parse_json_object(block["text"])
    return {}


def dates_block(today: date | None, cached_on: date | None) -> str:
    if today is None:
        return ""
    return _DATES.format(today=today.isoformat(), cached_on=cached_on.isoformat() if cached_on else "an unknown date")


def questions_equivalent(
    bedrock_client, *, model_id: str, cached_query: str, new_query: str, today: date | None = None, cached_on: date | None = None
) -> tuple[bool, str]:
    prompt = EQUIVALENCE_PROMPT.format(dates=dates_block(today, cached_on), cached_query=cached_query, new_query=new_query)
    out = _converse(bedrock_client, model_id, prompt)
    return bool(out.get("equivalent", False)), str(out.get("reason") or "")


def answer_satisfies(
    bedrock_client,
    *,
    model_id: str,
    cached_query: str,
    cached_answer: str,
    new_query: str,
    today: date | None = None,
    cached_on: date | None = None,
) -> tuple[bool, str]:
    out = _converse(
        bedrock_client,
        model_id,
        SATISFIES_PROMPT.format(
            dates=dates_block(today, cached_on), cached_query=cached_query, cached_answer=cached_answer, new_query=new_query
        ),
    )
    return bool(out.get("satisfies", False)), str(out.get("reason") or "")
