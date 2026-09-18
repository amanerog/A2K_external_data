"""Semantic answer cache for the live (agent-mediated) ask/search path
(mcp_to_agent_to_mcp branch) -- see gateway/engine.py's `_ask_via_agent()`/
`_search_via_agent()`, which call `lookup()` before `_call_agent()` and
`store()` after a successful agent-derived envelope is built.

Off unless `config.answer_cache_ready` (both `ANSWER_CACHE_ENABLED=true` and
`ANSWER_CACHE_TABLE` set) -- see config.py's own comments on why this starts
disabled by default.

Vendor-blind by design: the cache answers "has this question already been
answered", not "has this vendor already answered this question" -- `sources`
is never part of matching, only `operation` (`ask` vs `search`, since those
are different response contracts, not a routing choice) is. See this
branch's plan for the full reasoning; the short version is that which vendor
happened to answer a question is an implementation detail the cache doesn't
care about, not something the caller is asking about.

Three-step match, to keep semantic false positives ("who controls X" vs
"what does X control") from silently serving a wrong-but-plausible answer:
  1. Embed the incoming query (Bedrock Titan Text Embeddings v2) and rank
     stored entries by cosine similarity -- cheap, recall-favoring pre-filter
     (config.answer_cache_similarity_threshold, generous on purpose).
  2. One Bedrock Converse call checks whether the two *questions* are
     actually equivalent (not just the answer -- the question itself).
  3. A second, independent Bedrock Converse call checks whether the saved
     *answer* substantively satisfies the new question.
Both 2 and 3 must confirm -- see the comment above _EQUIVALENCE_PROMPT in
this file for why two independent checks, not one. Step 1 alone is never
trusted to serve a hit on its own.

What's cached is the raw `content` dict `_call_agent()` returns (the agent's
own response shape, from agent/entrypoint_a2k.py -- `answer`/`claims`/
`passages`/`citations`/`groundedRatio`/`conflicts`/`usage`/`toolCalls`), not
a pre-built envelope. This is deliberate: engine.py already knows how to
turn that exact shape into a full CitedResponseEnvelope (citation/claim
building, usage/toolCalls extraction -- `_citation_from_agent()`,
`_agent_usage()`, `_agent_tool_calls()`), for both `ask` (claims) and
`search` (passages) alike. A cache hit hands that same `content` shape back
so engine.py reuses that existing code unchanged, rather than this module
duplicating envelope-construction logic for two different operations.

Storage: DynamoDB, one item per cached answer, `id` (uuid) as partition key.
`content`/`embedding` are stored as a single JSON-encoded `payloadJson`
string attribute rather than native DynamoDB list/map types -- same reason
`a2k/cards/__init__.py`'s `cardJson` does this: it sidesteps boto3's
float/Decimal conversion requirement for the embedding vector entirely, at
the cost of not being able to query into those fields server-side (fine
here -- every read is a full-table scan already, see below). `ttl` is a real
DynamoDB Number attribute (required by DynamoDB's TTL feature) so expired
entries self-delete -- no cleanup job needed, though that deletion isn't
instant (DynamoDB documents up to 48h lag), so `lookup()` also filters
expired entries client-side rather than trusting it.

Honesty note on scale, same spirit as `cards/__init__.py` and
`gateway/audit.py`'s own "not real WORM storage" comment: reads are a full
`Scan` + in-process cosine-similarity ranking in Python, not a real vector
index (OpenSearch Serverless w/ vector engine, etc.). Fine at the
hundreds-to-low-thousands-of-entries scale this starts at. Not fine
indefinitely -- if this table grows large, replace the storage/lookup
internals here, not the lookup()/store() call sites in engine.py.
"""

from __future__ import annotations

import json
import math
import sys
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..config import config

# In-process cache of the last full table scan -- (expiry_epoch, entries),
# same lazy-refresh shape as cards/__init__.py's _vendor_cards_cache. Short
# on purpose: unlike the vendor catalogue (changes rarely), this table can
# gain a new entry on every live request, and a too-long window means a
# just-stored answer isn't visible to *other* concurrent requests until it
# expires. Not a config knob -- an internal performance detail, not
# something a deployment should need to tune.
_SCAN_CACHE_TTL_SECONDS = 30
_scan_cache: tuple[float, list[dict[str, Any]]] | None = None


@dataclass
class CachedAnswer:
    """What a cache hit gives engine.py -- `content` is handed straight to
    the same citation/claim/usage/toolCalls building engine.py already runs
    on a live `_call_agent()` result. `answeredBySources`/`cachedAt` are for
    observability (logs, the cache table itself) -- engine.py's own
    `source_kb_id` computation is driven by the *new* request's `sources`,
    same as a live call, so nothing here needs to override that."""

    content: dict[str, Any]
    answeredBySources: list[str]
    cachedAt: str  # ISO 8601 UTC, e.g. "2026-09-17T10:00:00Z"


@dataclass
class LookupResult:
    hit: CachedAnswer | None
    # Populated even on a miss -- engine.py passes this straight into
    # store() afterward so the query is only ever embedded once per request.
    query_embedding: list[float] = field(default_factory=list)


def _normalize_query(text: str) -> str:
    """Lowercase + collapse all whitespace runs to a single space, trimmed --
    applied before embedding and before storing `queryText`, so two questions
    that differ only in casing/spacing ("Who controls Acme Corp?" vs "who
    controls   acme corp?") embed to the exact same vector (similarity 1.0)
    instead of relying on the embedding model's own robustness to that.
    Called on both lookup() and store()'s `query` independently -- pure and
    deterministic, so both always agree without needing to pass a normalized
    value between them."""
    return " ".join(text.lower().split())


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


def embed_query(bedrock_client, text: str) -> list[float]:
    response = bedrock_client.invoke_model(
        modelId=config.answer_cache_embedding_model_id,
        body=json.dumps({"inputText": text}),
    )
    body = json.loads(response["body"].read())
    return body["embedding"]


# Two independent verification calls, both required (AND, not OR) before a
# candidate is served -- added 2026-09-18 after discussing that the single
# "does the answer satisfy the new question" call, while already the
# stronger of the two (it's grounded in the answer's actual content, not
# just question phrasing), is still one model's single judgment. This data
# is KYC/AML -- same reasoning behind running two differently-designed
# judges (weighted rubric + containment) in deploy/agentcore/judge.py rather
# than trusting one: an independent second opinion, framed around a
# different question, catches a different slice of mistakes than asking the
# same model the same way twice would. Costs a second Converse call on every
# candidate that clears the similarity threshold, and can reject a
# perfectly good hit that only failed the *equivalence* framing (e.g. the
# saved answer is broader than the new question needs) -- accepted
# deliberately: precision over hit-rate for this data. Revisit if that
# trade turns out not to be worth it in practice.

_EQUIVALENCE_PROMPT = """Decide whether these two questions are asking for the same underlying \
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

_SATISFIES_PROMPT = """A caller previously asked a question and received an answer, which was \
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


def _converse_json(bedrock_client, prompt: str) -> dict:
    response = bedrock_client.converse(
        modelId=config.answer_cache_verify_model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 200, "temperature": 0},
    )
    text = response["output"]["message"]["content"][0]["text"].strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    return json.loads(text)


def _questions_equivalent(bedrock_client, *, cached_query: str, new_query: str) -> bool:
    prompt = _EQUIVALENCE_PROMPT.format(cached_query=cached_query, new_query=new_query)
    return bool(_converse_json(bedrock_client, prompt).get("equivalent", False))


def _answer_satisfies(bedrock_client, *, cached_query: str, cached_answer: str, new_query: str) -> bool:
    prompt = _SATISFIES_PROMPT.format(cached_query=cached_query, cached_answer=cached_answer or "", new_query=new_query)
    return bool(_converse_json(bedrock_client, prompt).get("satisfies", False))


def _scan_entries() -> list[dict[str, Any]]:
    global _scan_cache
    now = time.monotonic()
    if _scan_cache is not None and _scan_cache[0] > now:
        return _scan_cache[1]

    import boto3

    table = boto3.resource("dynamodb").Table(config.answer_cache_table)
    items: list[dict[str, Any]] = []
    scan_kwargs: dict = {}
    while True:
        response = table.scan(**scan_kwargs)
        items.extend(response.get("Items", []))
        last_key = response.get("LastEvaluatedKey")
        if not last_key:
            break
        scan_kwargs["ExclusiveStartKey"] = last_key

    _scan_cache = (now + _SCAN_CACHE_TTL_SECONDS, items)
    return items


def lookup(bedrock_client, query: str, operation: str) -> LookupResult:
    """Never raises -- a broken/unreachable cache must fall through to a
    live agent call, not fail the request. Returns a miss (with the computed
    embedding still populated when embedding itself succeeded, empty
    otherwise) on any error, same resilience posture as gateway/audit.py's
    own best-effort local-file write.

    The embedding call is its own try/except, not folded into the block
    below -- confirmed live 2026-09-18 that leaving it unguarded meant a
    Bedrock-side failure here (e.g. a missing IAM permission) propagated all
    the way up through _ask_via_agent() and failed the entire a2k.ask call,
    exactly the outcome this module's whole design is supposed to prevent."""
    query = _normalize_query(query)
    try:
        query_embedding = embed_query(bedrock_client, query)
    except Exception as exc:  # noqa: BLE001 -- see docstring above
        print(f"a2k-box: answer cache embedding failed, falling through to a live call: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return LookupResult(hit=None, query_embedding=[])

    try:
        now_epoch = int(time.time())
        candidates = []
        for item in _scan_entries():
            if item.get("operation") != operation:
                continue
            if int(item.get("ttl", 0)) <= now_epoch:
                continue
            payload = json.loads(item["payloadJson"])
            similarity = _cosine_similarity(query_embedding, payload["embedding"])
            if similarity >= config.answer_cache_similarity_threshold:
                candidates.append((similarity, item, payload))

        if not candidates:
            return LookupResult(hit=None, query_embedding=query_embedding)

        candidates.sort(key=lambda c: c[0], reverse=True)
        _, best_item, best_payload = candidates[0]

        # Both checks must confirm -- see the comment above _EQUIVALENCE_PROMPT
        # for why this is AND, not OR. Equivalence first: cheaper to reject on
        # (no need to spend the second call once the first already says no).
        equivalent = _questions_equivalent(
            bedrock_client,
            cached_query=best_payload["queryText"],
            new_query=query,
        )
        if not equivalent:
            return LookupResult(hit=None, query_embedding=query_embedding)

        satisfies = _answer_satisfies(
            bedrock_client,
            cached_query=best_payload["queryText"],
            cached_answer=best_payload["content"].get("answer") or "",
            new_query=query,
        )
        if not satisfies:
            return LookupResult(hit=None, query_embedding=query_embedding)

        hit = CachedAnswer(
            content=best_payload["content"],
            answeredBySources=best_item.get("answeredBySources") or [],
            cachedAt=best_payload["cachedAt"],
        )
        return LookupResult(hit=hit, query_embedding=query_embedding)
    except Exception as exc:  # noqa: BLE001 -- cache errors must never break a live request
        # Always printed (not gated behind A2K_TRACE_CALLS) -- same reasoning
        # as gateway/audit.py's own best-effort-write warning: a silent
        # miss-on-every-call is an operational problem (bad IAM, wrong table
        # name, ...) worth surfacing in CloudWatch, not routine debug noise.
        print(f"a2k-box: answer cache lookup failed, falling through to a live call: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
        return LookupResult(hit=None, query_embedding=query_embedding)


def store(*, operation: str, query: str, query_embedding: list[float], answered_by_sources: list[str], content: dict[str, Any]) -> None:
    """Never raises -- a failed cache write must not fail an otherwise-
    successful response. Called after a live agent call succeeds, with the
    exact `content` dict `_call_agent()` returned; reuses the embedding
    lookup() already computed for this same request rather than re-embedding
    the query a second time.

    `query` is normalized the same way lookup() normalizes it -- pure and
    deterministic, so `queryText` stored here always matches what the
    `query_embedding` passed in was actually computed from."""
    query = _normalize_query(query)
    try:
        import boto3

        now = datetime.now(timezone.utc)
        cached_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
        payload = {
            "queryText": query,
            "embedding": query_embedding,
            "content": content,
            "cachedAt": cached_at,
        }
        item = {
            "id": str(uuid.uuid4()),
            "operation": operation,
            "cachedAt": cached_at,
            "ttl": int(now.timestamp()) + config.answer_cache_ttl_seconds,
            "answeredBySources": answered_by_sources,
            "payloadJson": json.dumps(payload, ensure_ascii=False, default=str),
        }
        table = boto3.resource("dynamodb").Table(config.answer_cache_table)
        table.put_item(Item=item)

        # Make the just-written entry visible to the next lookup on *this*
        # process without waiting out _SCAN_CACHE_TTL_SECONDS -- other,
        # concurrent processes still won't see it until their own scan cache
        # expires (acceptable staleness window, see module docstring).
        global _scan_cache
        if _scan_cache is not None:
            _scan_cache[1].append(item)
    except Exception as exc:  # noqa: BLE001 -- cache errors must never break a live request
        print(f"a2k-box: answer cache store failed, this answer won't be cached: {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
