"""Exercises SayariMcpAdapter's live branch against a fake `_call_tool` and
`_get_token` that mirror the confirmed shape at Sayari's real MCP server
(Auth0 client-credentials, search_entities -> double-JSON-encoded
{"result": "<JSON string>"}, get_entity_summary -> profile dict directly --
all verified live 2026-08-12, see adapters/sayari_mcp.py's module
docstring). No real network call and no AUTH0_CLIENT_ID/SECRET needed.
"""

from types import SimpleNamespace

import pytest

from a2k.adapters import sayari_mcp as sayari_mcp_module
from a2k.errors import A2KError, ErrorCode

ENTITY_ID = "Q2lTuvjEhN_xJ8jkq5nxMQ"


async def _fake_call_tool(self, session, tool, arguments):
    if tool == "search_entities":
        if arguments["query"] == "RateLimitMe":
            raise A2KError(ErrorCode.RATE_LIMITED, "Sayari rate limit hit -- search_entities", retryable=True)
        if arguments["query"] == "Sinopec":
            return {
                "tool": "search_entities",
                "returned": 1,
                "items": [
                    {
                        "entity_id": ENTITY_ID,
                        "label": "SINOPEC CHEMICAL COMMERCIAL INTERNATIONAL CO., LTD.",
                        "type": "company",
                        "quality_flags": ["sparse_identifiers", "missing_country"],
                        "next_action": {"tool": "get_entity_summary", "args": {"entity_id": ENTITY_ID}},
                    }
                ],
            }
        return {"tool": "search_entities", "returned": 0, "items": []}

    if tool == "get_entity_summary":
        assert arguments == {"entity_id": ENTITY_ID}
        return {
            "entity_id": ENTITY_ID,
            "label": "SINOPEC CHEMICAL COMMERCIAL INTERNATIONAL CO., LTD.",
            "type": "company",
            "closed": False,
            "attributes": {
                "names": ["SINOPEC CHEMICAL COMMERCIAL INTERNATIONAL CO.,LTD."],
                "countries": ["CHN", "RUS", "VNM", "HKG"],
                "company_type": "CO LTD",
            },
            "risk": {"sanctioned": False, "pep": False, "risk_levels": ["basel_aml", "cpi_score"]},
            "sources": ["Netherlands Imports & Exports (January 2023 - Present)"],
            "entity_url": f"https://graph.sayari.com/resource/entity/{ENTITY_ID}",
        }

    raise AssertionError(f"unexpected tool call: {tool}({arguments})")


class _FakeSessionCtx:
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
        sayari_mcp_url="https://mcp.sayari.com/mcp",
        sayari_auth0_client_id="test-client-id",
        sayari_auth0_client_secret="test-client-secret",
        sayari_mcp_live_ready=True,
        httpx_verify=True,
        max_entities_to_hydrate=3,
    )
    monkeypatch.setattr(sayari_mcp_module, "config", fake_config)
    monkeypatch.setattr(sayari_mcp_module.SayariMcpAdapter, "_call_tool", _fake_call_tool)
    monkeypatch.setattr(
        sayari_mcp_module.SayariMcpAdapter, "_open_session", lambda self, http_client: _FakeSessionCtx()
    )
    monkeypatch.setattr(sayari_mcp_module.SayariMcpAdapter, "_get_token", lambda self: _fake_get_token())
    monkeypatch.setattr(sayari_mcp_module, "ClientSession", _FakeClientSession)
    return sayari_mcp_module.SayariMcpAdapter()


async def _fake_get_token() -> str:
    return "fake-token"


async def test_live_search_matches_confirmed_sayari_mcp_schema(patched_adapter):
    facts = await patched_adapter._live_search("Sinopec", limit=10)

    assert {f.field for f in facts} == {"type", "closed", "names", "countries", "company_type", "sanctioned", "pep"}
    entity_url = f"https://graph.sayari.com/resource/entity/{ENTITY_ID}"
    assert all(f.source_url == entity_url for f in facts)
    assert all(f.entity_key == ENTITY_ID for f in facts)

    sanctioned_fact = next(f for f in facts if f.field == "sanctioned")
    assert sanctioned_fact.value == "False"  # kept, not filtered -- "no match" is still a citable claim

    countries_fact = next(f for f in facts if f.field == "countries")
    assert countries_fact.value == "CHN, RUS, VNM, HKG"


async def test_live_search_no_results_returns_empty(patched_adapter):
    facts = await patched_adapter._live_search("NoSuchCompany", limit=10)
    assert facts == []


async def test_rate_limit_maps_to_retryable_rate_limited_error(patched_adapter):
    with pytest.raises(A2KError) as excinfo:
        await patched_adapter._live_search("RateLimitMe", limit=10)
    assert excinfo.value.code == ErrorCode.RATE_LIMITED
    assert excinfo.value.retryable is True


async def test_live_search_without_credentials_raises_upstream_error(monkeypatch):
    fake_config = SimpleNamespace(sayari_auth0_client_id=None, sayari_auth0_client_secret=None, sayari_mcp_live_ready=False)
    monkeypatch.setattr(sayari_mcp_module, "config", fake_config)
    adapter = sayari_mcp_module.SayariMcpAdapter()

    with pytest.raises(A2KError) as excinfo:
        await adapter._live_search("Sinopec", limit=10)
    assert excinfo.value.code.value == "UPSTREAM_ERROR"


def test_facts_from_entity_summary_skips_blank_and_zero_length_values():
    summary = {
        "entity_id": "abc",
        "label": "Acme",
        "entity_url": "https://graph.sayari.com/resource/entity/abc",
        "attributes": {"company_type": "", "names": []},
        "risk": {"sanctioned": False, "pep": None},
    }
    facts = sayari_mcp_module.facts_from_entity_summary(summary, "urn:a2k:vendor:sayari")
    fields = {f.field for f in facts}
    # empty string, empty list, and None are all skipped; False is kept.
    assert fields == {"sanctioned"}
