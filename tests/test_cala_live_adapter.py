"""Exercises CalaAdapter's live branch against a fake HTTP transport that
mirrors the confirmed shape at api.cala.ai/openapi.json (X-API-KEY header,
GET /v1/entities -> {"entities": [...]}, GET .../introspection, POST
/v1/entities/{id} with an explicit `properties` list). No real network
call and no CALA_API_KEY needed -- this checks our parsing logic against
the documented contract, not the live service itself.
"""

import json
from types import SimpleNamespace

import httpx
import pytest

from a2k_box.adapters import cala as cala_module

ENTITY_ID = "e5bb591a-d308-4aa5-9672-96046d366cde"

_call_counts = {"introspection": 0}


@pytest.fixture(autouse=True)
def _reset_call_counts():
    _call_counts["introspection"] = 0
    yield


def _handler(request: httpx.Request) -> httpx.Response:
    assert request.headers["x-api-key"] == "test-key"

    if request.method == "GET" and request.url.path == "/v1/entities":
        name = request.url.params["name"]
        if name == "RateLimitMe":
            return httpx.Response(429, json={"detail": "rate limit exceeded"}, headers={"Retry-After": "30"})
        if name == "Acme":
            return httpx.Response(
                200,
                json={
                    "entities": [
                        {
                            "id": ENTITY_ID,
                            "name": "Acme Robotics Inc.",
                            "entity_type": "Company",
                            "description": "A robotics company.",
                        }
                    ]
                },
            )
        # No named entity matches -- e.g. a filter/listing query -- so
        # _live_search should fall back to POST /v1/knowledge/query.
        return httpx.Response(200, json={"entities": []})

    if request.method == "GET" and request.url.path == f"/v1/entities/{ENTITY_ID}/introspection":
        _call_counts["introspection"] += 1
        return httpx.Response(
            200,
            json={"properties": ["name", "employee_count"], "relationships": {}, "numerical_observations": {}},
        )

    if request.method == "GET" and request.url.path == "/v1/entities/does-not-exist/introspection":
        return httpx.Response(404, json={"detail": "not found"})

    if request.method == "POST" and request.url.path == f"/v1/entities/{ENTITY_ID}":
        body = json.loads(request.content)
        assert body == {"properties": ["name", "employee_count"]}
        return httpx.Response(
            200,
            json={
                "id": ENTITY_ID,
                "name": "Acme Robotics Inc.",
                "entity_type": "Company",
                "description": "A robotics company.",
                "properties": {
                    "name": {
                        "value": "Acme Robotics Inc.",
                        "sources": [{"date": "2026-01-15T00:00:00Z", "document": "SEC EDGAR", "name": "10-K Filing"}],
                    },
                    "employee_count": {"value": 250, "sources": []},
                },
                "relationships": {},
                "numerical_observations": [],
            },
        )

    if request.method == "POST" and request.url.path == "/v1/knowledge/query":
        body = json.loads(request.content)
        assert body == {"input": "startups in Spain with funding 10M-50M"}
        return httpx.Response(
            200,
            json={
                "results": [
                    {"name": "Luzia", "funding": "13M", "location": "Spain"},
                    {"name": "Nomad Solar", "funding": "15M", "location": "Spain"},
                ],
                "entities": None,
            },
        )

    return httpx.Response(404)


@pytest.fixture
def patched_adapter(monkeypatch):
    fake_config = SimpleNamespace(
        cala_api_key="test-key",
        cala_base_url="https://api.cala.ai",
        cala_live_ready=True,
        cala_introspection_cache_ttl_seconds=86400,
        httpx_verify=True,
    )
    monkeypatch.setattr(cala_module, "config", fake_config)

    real_async_client = httpx.AsyncClient

    def make_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(_handler)
        return real_async_client(*args, **kwargs)

    monkeypatch.setattr(cala_module.httpx, "AsyncClient", make_client)
    return cala_module.CalaAdapter()


async def test_live_search_matches_confirmed_cala_schema(patched_adapter):
    facts = await patched_adapter._live_search("Acme", limit=10)

    assert {f.field for f in facts} == {"name", "employee_count"}
    employee_fact = next(f for f in facts if f.field == "employee_count")
    assert employee_fact.value == "250"
    assert employee_fact.entity_name == "Acme Robotics Inc."
    assert employee_fact.entity_key == ENTITY_ID
    assert employee_fact.document_id == f"urn:a2k:vendor:cala:doc:{ENTITY_ID}:profile"

    name_fact = next(f for f in facts if f.field == "name")
    assert name_fact.title == "10-K Filing"
    assert name_fact.source_last_updated == "2026-01-15T00:00:00Z"
    # Cala's `sources` entries don't carry a resolvable URL -- must not fabricate one.
    assert name_fact.source_url is None


async def test_live_get_document_reuses_introspect_then_query(patched_adapter):
    document_id = f"urn:a2k:vendor:cala:doc:{ENTITY_ID}:profile"
    doc = await patched_adapter._live_get_document(document_id)

    assert doc is not None
    assert doc.document_id == document_id
    assert "Acme Robotics Inc." in doc.content
    assert "250" in doc.content


async def test_introspection_is_cached_across_searches_values_still_fresh(patched_adapter):
    first = await patched_adapter._live_search("Acme", limit=10)
    assert _call_counts["introspection"] == 1
    assert any(f.field == "employee_count" and f.value == "250" for f in first)

    # Second search for the same entity: schema (property names) comes from
    # cache -- no second introspection call -- but the values themselves are
    # still fetched fresh via POST /v1/entities/{id}, not reused from before.
    second = await patched_adapter._live_search("Acme", limit=10)
    assert _call_counts["introspection"] == 1
    assert any(f.field == "employee_count" and f.value == "250" for f in second)


async def test_introspection_cache_respects_ttl(patched_adapter):
    await patched_adapter._live_search("Acme", limit=10)
    assert _call_counts["introspection"] == 1

    cala_module.config.cala_introspection_cache_ttl_seconds = 0
    await patched_adapter._live_search("Acme", limit=10)
    assert _call_counts["introspection"] == 2


async def test_live_search_falls_back_to_knowledge_query_when_no_entity_matches(patched_adapter):
    facts = await patched_adapter._live_search("startups in Spain with funding 10M-50M", limit=10)

    entity_names = {f.entity_name for f in facts}
    assert entity_names == {"Luzia", "Nomad Solar"}

    luzia_funding = next(f for f in facts if f.entity_name == "Luzia" and f.field == "funding")
    assert luzia_funding.value == "13M"
    assert luzia_funding.entity_key == "query:luzia"
    assert luzia_funding.document_id == "urn:a2k:vendor:cala:doc:query:luzia:profile"
    # There's no addressable Cala resource behind a filter-query row --
    # nothing to fabricate a source URL from.
    assert luzia_funding.source_url is None


async def test_get_document_resolves_cached_query_result_row(patched_adapter):
    await patched_adapter._live_search("startups in Spain with funding 10M-50M", limit=10)

    doc = await patched_adapter._live_get_document("urn:a2k:vendor:cala:doc:query:luzia:profile")

    assert doc is not None
    assert "Luzia" in doc.content
    assert "13M" in doc.content


async def test_rate_limit_maps_to_retryable_rate_limited_error(patched_adapter):
    from a2k_box.errors import A2KError, ErrorCode

    with pytest.raises(A2KError) as excinfo:
        await patched_adapter._live_search("RateLimitMe", limit=10)
    assert excinfo.value.code == ErrorCode.RATE_LIMITED
    assert excinfo.value.retryable is True
    assert excinfo.value.details["retryAfterSeconds"] == "30"


async def test_introspection_404_returns_none_not_upstream_error(patched_adapter):
    async with httpx.AsyncClient(
        base_url="https://api.cala.ai", transport=httpx.MockTransport(_handler), headers={"X-API-KEY": "test-key"}
    ) as client:
        detail = await patched_adapter._fetch_entity_detail(client, "does-not-exist", "Ghost Corp")
    assert detail is None


async def test_live_search_without_api_key_raises_upstream_error(monkeypatch):
    fake_config = SimpleNamespace(cala_api_key=None, cala_base_url="https://api.cala.ai", cala_live_ready=False)
    monkeypatch.setattr(cala_module, "config", fake_config)
    adapter = cala_module.CalaAdapter()

    from a2k_box.errors import A2KError

    with pytest.raises(A2KError) as excinfo:
        await adapter._live_search("Acme", limit=10)
    assert excinfo.value.code.value == "UPSTREAM_ERROR"
