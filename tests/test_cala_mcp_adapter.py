"""Exercises CalaMcpAdapter's live branch against a fake `_call_tool` that
mirrors the confirmed shape at Cala's real MCP server (X-API-KEY auth,
entity_search -> {"entities": [...]}, entity_introspection, entity_retrieval
with an explicit `properties` list -- all verified live 2026-08-10/11, see
`adapters/cala_mcp.py`'s module docstring). No real network call and no
CALA_API_KEY needed.
"""

from types import SimpleNamespace

import pytest

from a2k.adapters import cala_mcp as cala_mcp_module
from a2k.errors import A2KError, ErrorCode

ENTITY_ID = "e5bb591a-d308-4aa5-9672-96046d366cde"

_call_counts = {"entity_introspection": 0}


@pytest.fixture(autouse=True)
def _reset_call_counts():
    _call_counts["entity_introspection"] = 0
    yield


async def _fake_call_tool(self, session, tool, arguments):
    """Stands in for CalaMcpAdapter._call_tool -- same responsibility as
    `_handler` in test_cala_live_adapter.py, one level higher (tool name +
    arguments instead of HTTP method + path), since MCP's `session` isn't a
    thing you can hand an httpx.MockTransport."""
    if tool == "entity_search":
        name = arguments["name"]
        if name == "RateLimitMe":
            raise A2KError(
                ErrorCode.RATE_LIMITED,
                "Cala rate limit hit -- entity_search: HTTP error 429: Too Many Requests",
                retryable=True,
            )
        if name == "Acme":
            return {
                "entities": [
                    {"id": ENTITY_ID, "name": "Acme Robotics Inc.", "entity_type": "Company", "description": "..."}
                ]
            }
        return {"entities": []}

    if tool == "entity_introspection":
        _call_counts["entity_introspection"] += 1
        if arguments["entity_id"] == "does-not-exist":
            return None
        return {"properties": ["name", "employee_count"], "relationships": {}, "numerical_observations": {}}

    if tool == "entity_retrieval":
        assert arguments["properties"] == ["name", "employee_count"]
        return {
            "id": ENTITY_ID,
            "name": "Acme Robotics Inc.",
            "entity_type": "Company",
            "properties": {
                "name": {
                    "value": "Acme Robotics Inc.",
                    "sources": [{"date": "2026-01-15T00:00:00Z", "document": "SEC EDGAR", "name": "10-K Filing"}],
                },
                "employee_count": {"value": 250, "sources": []},
            },
            "relationships": {},
            "numerical_observations": [],
        }

    if tool == "knowledge_query":
        assert arguments == {"input": "startups in Spain with funding 10M-50M"}
        return {
            "results": [
                {"name": "Luzia", "funding": "13M", "location": "Spain"},
                {"name": "Nomad Solar", "funding": "15M", "location": "Spain"},
            ],
            "entities": None,
        }

    if tool == "knowledge_search":
        # Shape confirmed live 2026-08-11 against a real "Who founded Apple
        # and when?" query -- see cala_mcp.py's module docstring.
        return {
            "content": "Apple was founded on April 1, 1976 by Steve Jobs, Steve Wozniak, and Ronald Wayne.",
            "explainability": [
                {
                    "content": "The founders of Apple are Steve Jobs, Steve Wozniak, and Ronald Wayne.",
                    "references": ["ctx-1"],
                },
                {
                    "content": "Apple's initial founding date is April 1, 1976.",
                    "references": ["ctx-1", "ctx-2"],
                },
                {"content": "", "references": ["ctx-2"]},  # blank content -- must be skipped, not faked
            ],
            "context": [
                {
                    "id": "ctx-1",
                    "content": "Entity: Apple. Properties: founding_date = 1977-01-03.",
                    "origins": [
                        {"source": {"name": "trucoteca", "url": "https://trucoteca.com/en/quien-fundo-apple"}}
                    ],
                },
                {
                    "id": "ctx-2",
                    "content": "Apple official site.",
                    "origins": [{"source": {"name": "Apple", "url": "https://www.apple.com/"}}],
                },
            ],
        }

    raise AssertionError(f"unexpected tool call: {tool}({arguments})")


class _FakeSessionCtx:
    """Stands in for `streamablehttp_client(...)` -- an async context manager
    yielding (read, write, _); the fake ClientSession below ignores them."""

    async def __aenter__(self):
        return (None, None, None)

    async def __aexit__(self, *exc_info):
        return False


class _FakeClientSession:
    def __init__(self, read, write):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc_info):
        return False

    async def initialize(self):
        return None


@pytest.fixture
def patched_adapter(monkeypatch):
    fake_config = SimpleNamespace(
        cala_api_key="test-key",
        cala_mcp_url="https://api.cala.ai/mcp/",
        cala_live_ready=True,
        cala_introspection_cache_ttl_seconds=86400,
        cala_search_mode="entity_first",
        max_entities_to_hydrate=3,
    )
    monkeypatch.setattr(cala_mcp_module, "config", fake_config)
    monkeypatch.setattr(cala_mcp_module.CalaMcpAdapter, "_call_tool", _fake_call_tool)
    monkeypatch.setattr(cala_mcp_module.CalaMcpAdapter, "_open_session", lambda self: _FakeSessionCtx())
    monkeypatch.setattr(cala_mcp_module, "ClientSession", _FakeClientSession)
    return cala_mcp_module.CalaMcpAdapter()


async def test_live_search_matches_confirmed_cala_mcp_schema(patched_adapter):
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
    assert name_fact.source_url is None


async def test_introspection_is_cached_across_searches_values_still_fresh(patched_adapter):
    first = await patched_adapter._live_search("Acme", limit=10)
    assert _call_counts["entity_introspection"] == 1
    assert any(f.field == "employee_count" and f.value == "250" for f in first)

    second = await patched_adapter._live_search("Acme", limit=10)
    assert _call_counts["entity_introspection"] == 1
    assert any(f.field == "employee_count" and f.value == "250" for f in second)


async def test_live_search_falls_back_to_knowledge_query_when_no_entity_matches(patched_adapter):
    facts = await patched_adapter._live_search("startups in Spain with funding 10M-50M", limit=10)

    entity_names = {f.entity_name for f in facts}
    assert entity_names == {"Luzia", "Nomad Solar"}

    luzia_funding = next(f for f in facts if f.entity_name == "Luzia" and f.field == "funding")
    assert luzia_funding.value == "13M"
    assert luzia_funding.entity_key == "query:luzia"
    assert luzia_funding.document_id == "urn:a2k:vendor:cala:doc:query:luzia:profile"
    assert luzia_funding.source_url is None


async def test_get_document_resolves_cached_query_result_row(patched_adapter):
    await patched_adapter._live_search("startups in Spain with funding 10M-50M", limit=10)

    doc = await patched_adapter._live_get_document("urn:a2k:vendor:cala:doc:query:luzia:profile")

    assert doc is not None
    assert "Luzia" in doc.content
    assert "13M" in doc.content


async def test_rate_limit_maps_to_retryable_rate_limited_error(patched_adapter):
    from a2k.errors import A2KError, ErrorCode

    with pytest.raises(A2KError) as excinfo:
        await patched_adapter._live_search("RateLimitMe", limit=10)
    assert excinfo.value.code == ErrorCode.RATE_LIMITED
    assert excinfo.value.retryable is True


async def test_knowledge_search_facts_quote_explainability_verbatim_and_cite_first_origin(patched_adapter):
    async with patched_adapter._open_session() as (read, write, _):
        async with cala_mcp_module.ClientSession(read, write) as session:
            facts = await patched_adapter._knowledge_search_facts(session, "Who founded Apple?", limit=10)

    # The blank-content explainability item must be skipped, not turned
    # into an empty/fabricated Fact.
    assert len(facts) == 2

    first = facts[0]
    assert first.text == "The founders of Apple are Steve Jobs, Steve Wozniak, and Ronald Wayne."
    assert first.value == first.text  # quoted verbatim -- groundedRatio stays honest
    assert first.source_url == "https://trucoteca.com/en/quien-fundo-apple"
    assert first.title == "trucoteca"

    second = facts[1]
    # References ["ctx-1", "ctx-2"] -- must cite the *first* referenced
    # context's *first* origin, same sources[0] convention as
    # facts_from_entity_response, not every source at once.
    assert second.source_url == "https://trucoteca.com/en/quien-fundo-apple"


async def test_search_mode_knowledge_search_only_skips_entity_flow_entirely(patched_adapter, monkeypatch):
    monkeypatch.setattr(cala_mcp_module.config, "cala_search_mode", "knowledge_search_only")

    facts = await patched_adapter._live_search("Acme", limit=10)

    # "Acme" would normally match via entity_search (see the "Acme" branch
    # in _fake_call_tool) -- knowledge_search_only must never call it.
    assert all(f.entity_key.startswith("knowledge_search:") for f in facts)


async def test_search_mode_parallel_merges_entity_and_knowledge_search_facts(patched_adapter, monkeypatch):
    monkeypatch.setattr(cala_mcp_module.config, "cala_search_mode", "parallel")

    facts = await patched_adapter._live_search("Acme", limit=10)

    entity_facts = [f for f in facts if f.entity_key == ENTITY_ID]
    ks_facts = [f for f in facts if f.entity_key.startswith("knowledge_search:")]
    assert entity_facts and ks_facts, "parallel mode must merge facts from both tools, not pick one"


async def test_search_mode_entity_first_only_reaches_knowledge_search_as_last_resort(patched_adapter):
    # "Acme" matches entity_search, so entity_first must never touch
    # knowledge_search for this query in the default mode.
    facts = await patched_adapter._live_search("Acme", limit=10)
    assert all(f.entity_key == ENTITY_ID for f in facts)


async def test_entity_introspection_404_returns_none_not_upstream_error(patched_adapter):
    async with patched_adapter._open_session() as (read, write, _):
        async with cala_mcp_module.ClientSession(read, write) as session:
            detail = await patched_adapter._fetch_entity_detail(session, "does-not-exist", "Ghost Corp")
    assert detail is None


async def test_live_search_without_api_key_raises_upstream_error(monkeypatch):
    fake_config = SimpleNamespace(cala_api_key=None, cala_mcp_url="https://api.cala.ai/mcp/", cala_live_ready=False)
    monkeypatch.setattr(cala_mcp_module, "config", fake_config)
    adapter = cala_mcp_module.CalaMcpAdapter()

    from a2k.errors import A2KError

    with pytest.raises(A2KError) as excinfo:
        await adapter._live_search("Acme", limit=10)
    assert excinfo.value.code.value == "UPSTREAM_ERROR"
