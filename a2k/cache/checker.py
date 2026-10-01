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

from .bedrock import parse_json_object

PROMPT_VERSION = "checker-v1-as-tested"

EQUIVALENCE_PROMPT = """Decide whether these two questions are asking for the same underlying \
information -- not just topically similar, but such that a correct answer to one would also be \
a correct, complete answer to the other.

Previously asked question: {cached_query}

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

Previously asked question: {cached_query}

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


def questions_equivalent(bedrock_client, *, model_id: str, cached_query: str, new_query: str) -> tuple[bool, str]:
    out = _converse(bedrock_client, model_id, EQUIVALENCE_PROMPT.format(cached_query=cached_query, new_query=new_query))
    return bool(out.get("equivalent", False)), str(out.get("reason") or "")


def answer_satisfies(bedrock_client, *, model_id: str, cached_query: str, cached_answer: str, new_query: str) -> tuple[bool, str]:
    out = _converse(
        bedrock_client,
        model_id,
        SATISFIES_PROMPT.format(cached_query=cached_query, cached_answer=cached_answer, new_query=new_query),
    )
    return bool(out.get("satisfies", False)), str(out.get("reason") or "")
