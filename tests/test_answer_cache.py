"""Tests for a2k/gateway/cache.py's semantic answer cache -- the
similarity-ranking/threshold/verification logic, and the vendor-blind,
operation-exact matching behavior this branch's plan specifically calls for.

No AWS: boto3.resource("dynamodb") is monkeypatched with an in-memory fake
table (same pattern as tests/test_cards_dynamo.py), and a fake Bedrock
client stands in for the embedding call and both verification Converse
calls (question-equivalence and answer-satisfies, see cache.py) --
embeddings are looked up from an explicit dict rather than derived from the
query text, so each test controls its own cosine-similarity outcome exactly
rather than relying on incidental hash behavior.
"""

from __future__ import annotations

import json
import time
from types import SimpleNamespace

import boto3
import pytest

import a2k.gateway.cache as cache_module


class _FakeTable:
    def __init__(self):
        self.items: list[dict] = []

    def scan(self, **kwargs):
        return {"Items": list(self.items)}

    def put_item(self, Item):
        self.items.append(Item)


class _FakeDynamoResource:
    def __init__(self, table: _FakeTable):
        self._table = table

    def Table(self, name):
        return self._table


class _FakeBedrock:
    """`embeddings` maps query text -> a fixed vector, so similarity between
    any two queries in a test is exactly under the test's control.

    There are now two independent verification calls per candidate --
    "are these questions equivalent?" and "does the saved answer satisfy
    the new question?" (see cache.py's comment above _EQUIVALENCE_PROMPT
    for why both, not one). This fake tells them apart by which JSON key
    each prompt asks for (`"equivalent"` vs `"satisfies"`, both literally
    present in their respective prompt templates) and answers each from its
    own independently-controllable result/queue, so a test can make one
    check pass and the other fail."""

    def __init__(
        self,
        embeddings: dict[str, list[float]],
        equivalent_result: bool = True,
        satisfies_result: bool = True,
        equivalent_queue: list[bool] | None = None,
        satisfies_queue: list[bool] | None = None,
    ):
        self.embeddings = embeddings
        self.equivalent_result = equivalent_result
        self.satisfies_result = satisfies_result
        self.equivalent_queue = list(equivalent_queue) if equivalent_queue else []
        self.satisfies_queue = list(satisfies_queue) if satisfies_queue else []
        self.verify_calls: list[dict] = []

    def invoke_model(self, modelId, body):
        text = json.loads(body)["inputText"]
        vector = self.embeddings[text]
        payload = json.dumps({"embedding": vector}).encode()
        return {"body": SimpleNamespace(read=lambda: payload)}

    def converse(self, modelId, messages, inferenceConfig):
        prompt = messages[0]["content"][0]["text"]
        self.verify_calls.append({"modelId": modelId, "prompt": prompt})
        if '"equivalent"' in prompt:
            value = self.equivalent_queue.pop(0) if self.equivalent_queue else self.equivalent_result
            body = json.dumps({"equivalent": value, "reason": "test"})
        else:
            assert '"satisfies"' in prompt
            value = self.satisfies_queue.pop(0) if self.satisfies_queue else self.satisfies_result
            body = json.dumps({"satisfies": value, "reason": "test"})
        return {"output": {"message": {"content": [{"text": body}]}}}


@pytest.fixture(autouse=True)
def _reset_scan_cache(monkeypatch):
    """The module-level scan cache must not leak between tests."""
    monkeypatch.setattr(cache_module, "_scan_cache", None)
    yield
    monkeypatch.setattr(cache_module, "_scan_cache", None)


def _use_fake_dynamo(monkeypatch, *, similarity_threshold: float = 0.90, ttl_seconds: int = 86400) -> _FakeTable:
    fake_table = _FakeTable()
    monkeypatch.setattr(
        cache_module,
        "config",
        SimpleNamespace(
            answer_cache_table="test-answer-cache",
            answer_cache_similarity_threshold=similarity_threshold,
            answer_cache_embedding_model_id="amazon.titan-embed-text-v2:0",
            answer_cache_verify_model_id="test-verify-model",
            answer_cache_ttl_seconds=ttl_seconds,
        ),
    )
    monkeypatch.setattr(boto3, "resource", lambda name: _FakeDynamoResource(fake_table))
    return fake_table


def _store_one(monkeypatch, bedrock, *, operation="ask", query="who controls acme corp?", content=None):
    """Mirrors the real flow (engine.py's lookup() computing the embedding,
    store() reusing it) -- query is normalized *before* embedding here too,
    same as lookup() does internally, so `embeddings` fixtures below only
    ever need an entry keyed by the already-normalized text."""
    content = content or {"answer": "Beta Holdings controls Acme Corp.", "citations": [], "toolCalls": [{"vendor": "cala"}]}
    normalized = cache_module._normalize_query(query)
    embedding = cache_module.embed_query(bedrock, normalized)
    cache_module.store(
        operation=operation,
        query=query,
        query_embedding=embedding,
        answered_by_sources=["cala"],
        content=content,
    )
    return content


def test_lookup_misses_on_an_empty_cache(monkeypatch):
    _use_fake_dynamo(monkeypatch)
    bedrock = _FakeBedrock(embeddings={"who controls acme corp?": [1.0, 0.0]})

    result = cache_module.lookup(bedrock, "who controls acme corp?", "ask")

    assert result.hit is None
    assert result.query_embedding == [1.0, 0.0]  # embedded even on a miss, for a caller's later store()


def test_lookup_hits_on_a_near_identical_query(monkeypatch):
    _use_fake_dynamo(monkeypatch)
    bedrock = _FakeBedrock(embeddings={
        "who controls acme corp?": [1.0, 0.0],
        "who controls acme corp": [0.99, 0.14],  # cosine sim ~0.99, above threshold
    })
    _store_one(monkeypatch, bedrock, query="who controls acme corp?")

    result = cache_module.lookup(bedrock, "who controls acme corp", "ask")

    assert result.hit is not None
    assert result.hit.content["answer"] == "Beta Holdings controls Acme Corp."
    assert result.hit.answeredBySources == ["cala"]


def test_lookup_misses_below_the_similarity_threshold(monkeypatch):
    _use_fake_dynamo(monkeypatch, similarity_threshold=0.90)
    bedrock = _FakeBedrock(embeddings={
        "who controls acme corp?": [1.0, 0.0],
        "what is the weather today?": [0.0, 1.0],  # orthogonal, similarity 0.0
    })
    _store_one(monkeypatch, bedrock, query="who controls acme corp?")

    result = cache_module.lookup(bedrock, "What is the weather today?", "ask")

    assert result.hit is None


def test_equivalence_check_rejects_a_semantically_similar_but_wrong_candidate(monkeypatch):
    """The concrete false-positive case this whole design exists for: "who
    controls X" vs "what does X control" embed close together but ask for
    opposite things -- the embedding stage alone would hit, the equivalence
    check must be the one that catches it (and short-circuits before the
    second, satisfies check even runs)."""
    _use_fake_dynamo(monkeypatch, similarity_threshold=0.90)
    bedrock = _FakeBedrock(
        embeddings={
            "who controls acme corp?": [1.0, 0.0],
            "what does acme corp control?": [0.99, 0.14],
        },
        equivalent_result=False,
    )
    _store_one(monkeypatch, bedrock, query="who controls acme corp?")

    result = cache_module.lookup(bedrock, "What does Acme Corp control?", "ask")

    assert result.hit is None
    assert len(bedrock.verify_calls) == 1  # rejected on equivalence -- satisfies was never even asked


def test_satisfies_check_alone_rejecting_is_also_enough_to_miss(monkeypatch):
    """Both checks must confirm (AND, not OR) -- equivalence passing on its
    own must not be enough to serve a hit if the answer itself doesn't hold
    up under the second, independent check."""
    _use_fake_dynamo(monkeypatch, similarity_threshold=0.90)
    bedrock = _FakeBedrock(
        embeddings={
            "who controls acme corp?": [1.0, 0.0],
            "who controls acme corp": [0.99, 0.14],
        },
        equivalent_result=True,
        satisfies_result=False,
    )
    _store_one(monkeypatch, bedrock, query="who controls acme corp?")

    result = cache_module.lookup(bedrock, "who controls acme corp", "ask")

    assert result.hit is None
    assert len(bedrock.verify_calls) == 2  # both checks ran; the second is what rejected it


def test_store_truncates_tool_call_outputs_before_writing(monkeypatch):
    """Found live 2026-09-22: some vendors' tool outputs (Sayari's
    search_entities under response_mode="export", the known repeat
    offender) run to hundreds of KB, which alone can push a DynamoDB item
    past its 400KB hard limit and fail PutItem with ValidationException.
    `output` is truncated (not dropped) so a cache-hit answer still keeps a
    bounded sample of the raw vendor response worth auditing against --
    everything else on the tool call (vendor/toolName/status/input) must
    still survive untouched."""
    fake_table = _use_fake_dynamo(monkeypatch)
    content = {
        "answer": "x",
        "citations": [],
        "toolCalls": [
            {"vendor": "sayari", "toolName": "search_entities", "status": "success", "input": {"q": "Acme"}, "output": "y" * 500_000},
        ],
    }
    cache_module.store(operation="ask", query="who controls acme?", query_embedding=[1.0, 0.0], answered_by_sources=["sayari"], content=content)

    assert len(fake_table.items) == 1
    stored_payload = json.loads(fake_table.items[0]["payloadJson"])
    stored_tool_call = stored_payload["content"]["toolCalls"][0]
    assert stored_tool_call["output"].startswith("y" * 100)
    assert len(stored_tool_call["output"]) < 5_200  # capped, not the full 500,000 chars
    assert "truncated" in stored_tool_call["output"]
    assert stored_tool_call["vendor"] == "sayari"
    assert stored_tool_call["toolName"] == "search_entities"
    assert stored_tool_call["input"] == {"q": "Acme"}
    assert len(fake_table.items[0]["payloadJson"]) < 10_000  # nowhere near the 400KB limit anymore


def test_store_truncates_dict_shaped_tool_call_output(monkeypatch):
    """The real path (observability.py's ToolCallLogger) stores `output` as
    a dict -- the tool's raw parsed result, not a pre-serialized string --
    so the oversized-payload case above (a giant string) doesn't exercise
    the actual code path. This is the one that does."""
    fake_table = _use_fake_dynamo(monkeypatch)
    content = {
        "answer": "x",
        "citations": [],
        "toolCalls": [
            {
                "vendor": "sayari",
                "toolName": "search_entities",
                "status": "success",
                "input": {"q": "Acme"},
                "output": {"entities": [{"id": str(i), "name": "y" * 200} for i in range(2_000)]},
            },
        ],
    }
    cache_module.store(operation="ask", query="who controls acme?", query_embedding=[1.0, 0.0], answered_by_sources=["sayari"], content=content)

    stored_payload = json.loads(fake_table.items[0]["payloadJson"])
    stored_output = stored_payload["content"]["toolCalls"][0]["output"]
    assert isinstance(stored_output, str)  # truncated to a preview string, not the full dict
    assert len(stored_output) < 5_200
    assert "truncated" in stored_output
    assert len(fake_table.items[0]["payloadJson"]) < 15_000


def test_search_results_can_hit_even_though_they_have_no_answer_field(monkeypatch):
    """Found live 2026-09-22: a `search` result's content carries `passages`
    and no `answer` at all, so reading `answer` unconditionally handed the
    satisfies check an empty string -- which it correctly judged as
    satisfying nothing, making a `search` cache hit impossible. The check
    must be given the passages' own text instead."""
    _use_fake_dynamo(monkeypatch)
    seen_prompts = []

    class _RecordingBedrock(_FakeBedrock):
        def converse(self, modelId, messages, inferenceConfig):
            seen_prompts.append(messages[0]["content"][0]["text"])
            return super().converse(modelId, messages, inferenceConfig)

    bedrock = _RecordingBedrock(embeddings={"who controls santander?": [1.0, 0.0]})
    search_content = {
        "passages": [{"text": "Banco Santander SA has no single controlling shareholder."}],
        "citations": [{"documentId": "d1"}],
    }
    _store_one(monkeypatch, bedrock, operation="search", query="who controls santander?", content=search_content)

    result = cache_module.lookup(bedrock, "who controls santander?", "search")

    assert result.hit is not None
    satisfies_prompt = next(p for p in seen_prompts if '"satisfies"' in p)
    assert "no single controlling shareholder" in satisfies_prompt  # real text, not an empty answer


def test_query_normalization_makes_pure_formatting_variants_embed_identically(monkeypatch):
    """Casing/whitespace-only differences must not depend on the embedding
    model's own robustness -- normalize first, so the exact same text (and
    therefore the exact same vector) is what actually gets embedded."""
    seen_inputs = []

    class _RecordingBedrock(_FakeBedrock):
        def invoke_model(self, modelId, body):
            text = json.loads(body)["inputText"]
            seen_inputs.append(text)
            return super().invoke_model(modelId, body)

    bedrock = _RecordingBedrock(embeddings={"who controls acme corp?": [1.0, 0.0]})

    cache_module.lookup(bedrock, "  Who Controls   Acme Corp?  ", "ask")

    assert seen_inputs == ["who controls acme corp?"]


def test_operation_is_an_exact_match_dimension(monkeypatch):
    """ask and search are different response contracts -- a stored `ask`
    answer must never satisfy a `search` lookup, even for the identical
    query text with a perfect similarity score."""
    _use_fake_dynamo(monkeypatch)
    bedrock = _FakeBedrock(embeddings={"who controls acme corp?": [1.0, 0.0]})
    _store_one(monkeypatch, bedrock, operation="ask", query="who controls acme corp?")

    result = cache_module.lookup(bedrock, "who controls acme corp?", "search")

    assert result.hit is None


def test_cache_is_vendor_blind(monkeypatch):
    """The core design correction this branch's plan made: the cache never
    looks at which vendor produced an answer, or what the new lookup's
    (nonexistent, at this layer) vendor scope would be -- a match is purely
    on question + operation. answeredBySources is carried through as
    metadata only, confirming provenance isn't lost, just never filtered on."""
    _use_fake_dynamo(monkeypatch)
    bedrock = _FakeBedrock(embeddings={"who controls acme corp?": [1.0, 0.0]})
    _store_one(monkeypatch, bedrock, query="who controls acme corp?", content={"answer": "x", "citations": [], "toolCalls": [{"vendor": "sayari"}]})

    result = cache_module.lookup(bedrock, "who controls acme corp?", "ask")

    assert result.hit is not None
    assert result.hit.answeredBySources == ["cala"]  # from store()'s explicit answered_by_sources, not content


def test_expired_entries_are_not_served(monkeypatch):
    fake_table = _use_fake_dynamo(monkeypatch)
    bedrock = _FakeBedrock(embeddings={"who controls acme corp?": [1.0, 0.0]})
    _store_one(monkeypatch, bedrock, query="who controls acme corp?")
    fake_table.items[0]["ttl"] = int(time.time()) - 10  # force it into the past

    result = cache_module.lookup(bedrock, "who controls acme corp?", "ask")

    assert result.hit is None


def test_lookup_never_raises_when_dynamo_is_unreachable(monkeypatch):
    """A broken/unreachable cache must fall through to a live agent call,
    never fail the request that's asking."""
    _use_fake_dynamo(monkeypatch)
    bedrock = _FakeBedrock(embeddings={"who controls acme corp?": [1.0, 0.0]})

    def _boom(name):
        raise RuntimeError("dynamo is down")

    monkeypatch.setattr(boto3, "resource", _boom)

    result = cache_module.lookup(bedrock, "who controls acme corp?", "ask")

    assert result.hit is None
    assert result.query_embedding == [1.0, 0.0]  # the embedding call itself succeeded fine here


def test_lookup_never_raises_when_embedding_itself_fails(monkeypatch):
    """The concrete bug found live 2026-09-18: a Bedrock-side failure on the
    embedding call (there, a missing IAM permission) must degrade to a plain
    miss, not propagate up and fail the entire a2k.ask call -- this is
    exactly the guarantee the rest of this module already had everywhere
    except here, before this test/fix existed."""
    _use_fake_dynamo(monkeypatch)

    class _BrokenEmbeddingBedrock(_FakeBedrock):
        def invoke_model(self, modelId, body):
            raise RuntimeError("AccessDeniedException: not authorized to perform bedrock:InvokeModel")

    bedrock = _BrokenEmbeddingBedrock(embeddings={})

    result = cache_module.lookup(bedrock, "who controls acme corp?", "ask")  # must not raise

    assert result.hit is None
    assert result.query_embedding == []  # nothing to reuse for a later store() -- embedding never succeeded


def test_store_never_raises_when_dynamo_is_unreachable(monkeypatch):
    _use_fake_dynamo(monkeypatch)

    def _boom(name):
        raise RuntimeError("dynamo is down")

    monkeypatch.setattr(boto3, "resource", _boom)

    cache_module.store(
        operation="ask",
        query="who controls acme corp?",
        query_embedding=[1.0, 0.0],
        answered_by_sources=["cala"],
        content={"answer": "x"},
    )  # must not raise
