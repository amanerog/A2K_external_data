"""Tests for what a cache hit saves (a2k/cache/savings.py) and the logging
behind the dashboard's saving and data-universe panels: the `answer` and
`avoided` blocks of cache.retrieval, the saving metrics, answer_cost stored
with an entry and the entities of cache.admission. Network-free.
"""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from a2k.cache import savings
from a2k.cache.store import MemoryStore
from a2k.config import config
from a2k.gateway.engine import GatewayEngine
from a2k.models.request import A2KRequest
from test_cache_admission import CLASSIFICATION, _admit, make_candidate
from test_cache_retrieval import HIT, VENDOR_CONTENT, _metric_lines, _serving_log, gateway  # noqa: F401 -- gateway is a fixture

AGENT_CONTENT = {
    **VENDOR_CONTENT,
    "toolCalls": [
        {"vendor": "cala", "toolName": "knowledge_search", "status": "success", "output": "https://www.cnmv.es/x https://cnmv.es/y"},
        {"vendor": "cala", "toolName": "entity_retrieval", "status": "success", "output": "https://www.boe.es/z"},
    ],
    "usage": {"inputTokens": 80_000, "outputTokens": 2_000, "totalTokens": 82_000},
    "coverage": "partial",
    "citations": [
        {"title": "a", "sourceUrl": "https://www.cnmv.es/x"},
        {"title": "b", "sourceUrl": "https://cnmv.es/y"},
        {"title": "c", "sourceUrl": "https://www.boe.es/z"},
        {"title": "invented", "sourceUrl": "https://made-up.example/p"},
    ],
}
COST = {"vendor_calls": {"cala": 2}, "agent_input_tokens": 80_000, "agent_output_tokens": 2_000}


@pytest.fixture
def prices(monkeypatch):
    """Cala at 0.05 USD per call, Linkup unpriced, the agent on Sonnet 4.5 (3/15 per M)."""
    monkeypatch.setattr(savings, "vendor_prices", lambda: ("test-v1", {"cala": 0.05, "linkup": None}))
    monkeypatch.setitem(config.__dict__, "agent_model_id", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")


@pytest.fixture
def agent_answer(gateway, monkeypatch):  # noqa: F811
    async def fake_call_agent(self, operation, query, sources, request_id):
        gateway[1]["vendor_calls"] += 1
        return dict(AGENT_CONTENT)

    monkeypatch.setattr(GatewayEngine, "_call_agent", fake_call_agent)
    return gateway


# --- savings -------------------------------------------------------------


def test_the_shipped_price_file_loads():
    version, prices = savings.vendor_prices()
    assert version and {"cala", "linkup"} <= set(prices)
    # Flat rate with no quota: a Linkup call avoided saves no money.
    assert prices["linkup"] == 0


def test_eur_prices_are_converted_to_usd(monkeypatch, tmp_path):
    prices_file = tmp_path / "prices.json"
    prices_file.write_text(json.dumps({"price_version": "v", "eur_usd": 1.2, "eur_per_call": {"cala": 0.5, "x": None}, "usd_per_call": {"y": 0.1}}))
    monkeypatch.setattr(savings, "PRICES_FILE", prices_file)
    savings.vendor_prices.cache_clear()
    try:
        assert savings.vendor_prices() == ("v", {"y": 0.1, "cala": 0.6, "x": None})
    finally:
        savings.vendor_prices.cache_clear()


def test_a_flat_rate_vendor_is_priced_at_zero_not_unpriced(monkeypatch):
    monkeypatch.setattr(savings, "vendor_prices", lambda: ("v", {"linkup": 0}))
    p = savings.priced({"vendor_calls": {"linkup": 4}, "agent_input_tokens": 0, "agent_output_tokens": 0})
    assert (p["vendor_calls"], p["vendor_cost_usd"], p["unpriced"]) == (4, 0.0, [])


def test_a_broken_price_file_leaves_every_vendor_unpriced(monkeypatch, tmp_path):
    monkeypatch.setattr(savings, "PRICES_FILE", tmp_path / "missing.json")
    savings.vendor_prices.cache_clear()
    try:
        assert savings.vendor_prices() == ("unavailable (FileNotFoundError)", {})
    finally:
        savings.vendor_prices.cache_clear()


def test_answer_cost_counts_calls_per_vendor_and_the_agent_tokens():
    content = {**AGENT_CONTENT, "toolCalls": [*AGENT_CONTENT["toolCalls"], {"vendor": "linkup", "toolName": "search"}, {"toolName": "AskContent"}]}
    assert savings.answer_cost(content) == {"vendor_calls": {"cala": 2, "linkup": 1}, "agent_input_tokens": 80_000, "agent_output_tokens": 2_000}


def test_priced_answer_cost(prices):
    p = savings.priced(COST)
    assert (p["vendor_calls"], p["vendor_cost_usd"], p["unpriced"], p["price_version"]) == (2, 0.1, [], "test-v1")
    assert p["agent_cost_usd"] == pytest.approx(80_000 * 3 / 1e6 + 2_000 * 15 / 1e6)


def test_a_vendor_with_no_price_counts_its_calls_but_no_cost(prices):
    p = savings.priced({"vendor_calls": {"linkup": 3}, "agent_input_tokens": 0, "agent_output_tokens": 0})
    assert (p["vendor_calls"], p["vendor_cost_usd"], p["unpriced"]) == (3, 0.0, ["linkup"])


def test_avoided_is_real_when_served_potential_for_a_would_be_hit(prices):
    real = savings.avoided(served_from="cache", would_hit=True, ask_cost=None, entry_cost=COST)
    assert real["kind"] == "real" and real["vendor_calls"] == 2
    # Shadow: what this ask's vendor answer took, not what the entry took.
    ask = {"vendor_calls": {"cala": 5}, "agent_input_tokens": 1, "agent_output_tokens": 1}
    potential = savings.avoided(served_from="vendor", would_hit=True, ask_cost=ask, entry_cost=COST)
    assert potential["kind"] == "potential" and potential["vendor_calls"] == 5
    # The vendor failed: what the entry took.
    assert savings.avoided(served_from="vendor_error", would_hit=True, ask_cost=None, entry_cost=COST)["vendor_calls"] == 2


def test_nothing_avoided_on_a_miss_or_with_no_known_cost(prices):
    assert savings.avoided(served_from="vendor", would_hit=False, ask_cost=COST, entry_cost=None) is None
    # An entry admitted before answer_cost was recorded.
    assert savings.avoided(served_from="cache", would_hit=True, ask_cost=None, entry_cost=None) is None


def test_citation_hosts_are_web_domains_without_www():
    content = {"citations": [{"sourceUrl": "https://www.cnmv.es/x"}, {"sourceUrl": "https://cnmv.es/y"}, {"sourceUrl": None}, {"documentId": "d"}]}
    assert savings.citation_hosts(content) == ["cnmv.es"]


# --- serving log and metrics ---------------------------------------------


async def test_shadow_would_hit_logs_the_answer_and_its_potential_saving(agent_answer, prices, capsys):
    engine, _ = agent_answer
    await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    out = capsys.readouterr().out
    (log,) = [x for x in map(json.loads, out.splitlines()) if x.get("event") == "cache.retrieval"]
    # Only the citations a vendor tool returned: the invented one is dropped before.
    agent_usd = round(80_000 * 3 / 1e6 + 2_000 * 15 / 1e6, 6)
    assert log["answer"] == {
        **COST,
        "vendor_cost_usd": 0.1,
        "agent_cost_usd": agent_usd,
        "unpriced": [],
        "coverage": "partial",
        "citation_hosts": ["boe.es", "cnmv.es"],
    }
    assert log["avoided"]["kind"] == "potential" and log["avoided"]["vendor_cost_usd"] == 0.1
    (m,) = [x for x in map(json.loads, out.splitlines()) if x.get("event") == "cache.metrics"]
    assert (m["PotentialVendorCallsAvoided"], m["PotentialVendorCostAvoided"], m["AvoidedVendor"], m["PriceVersion"]) == (2, 0.1, "cala", "test-v1")
    assert m["PotentialAgentCostAvoided"] == log["avoided"]["agent_cost_usd"] > 0
    # What the answer cost, as values for Logs Insights -- not metrics.
    assert (m["AnswerVendors"], m["AnswerVendorCalls"], m["AnswerVendorCostUSD"], m["AnswerAgentCostUSD"]) == ("cala", 2, 0.1, agent_usd)
    assert not any(x["Name"].startswith("Answer") for d in m["_aws"]["CloudWatchMetrics"] for x in d["Metrics"])
    assert "VendorCallsAvoided" not in m
    saving = m["_aws"]["CloudWatchMetrics"][-1]
    assert saving["Dimensions"] == [["Mode"]]
    assert [x["Name"] for x in saving["Metrics"]] == ["PotentialVendorCallsAvoided", "PotentialVendorCostAvoided", "PotentialAgentCostAvoided"]


async def test_serve_hit_logs_the_real_saving_from_the_entry(gateway, prices, capsys):  # noqa: F811
    engine, state = gateway
    state["mode"], state["hit"] = "serve", replace(HIT, vendor="cala", answer_cost=COST)
    await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    lines = [json.loads(x) for x in capsys.readouterr().out.splitlines() if x.startswith("{")]
    (log,) = [x for x in lines if x.get("event") == "cache.retrieval"]
    assert log["answer"] is None and log["entry_vendor"] == "cala"
    assert log["avoided"]["kind"] == "real" and log["avoided"]["vendor_calls"] == 2
    (m,) = [x for x in lines if x.get("event") == "cache.metrics"]
    assert (m["VendorCallsAvoided"], m["VendorCostAvoided"], m["AvoidedVendor"]) == (2, 0.1, "cala")


async def test_an_unpriced_vendor_emits_calls_but_no_vendor_cost(agent_answer, monkeypatch, capsys):
    monkeypatch.setattr(savings, "vendor_prices", lambda: ("v0", {"cala": None}))
    await agent_answer[0]._ask_via_agent(A2KRequest(operation="ask", query="q"))
    (m,) = _metric_lines(capsys)
    assert m["PotentialVendorCallsAvoided"] == 2 and "PotentialVendorCostAvoided" not in m
    assert "AnswerVendorCostUSD" not in m and m["AnswerAgentCostUSD"] > 0
    # Every declared metric is a member of the line (EMF rejects it otherwise).
    assert all(x["Name"] in m for d in m["_aws"]["CloudWatchMetrics"] for x in d["Metrics"])


async def test_a_miss_logs_the_answer_and_no_saving(agent_answer, prices, capsys):
    engine, state = agent_answer
    state["hit"] = None
    await engine._ask_via_agent(A2KRequest(operation="ask", query="q"))
    (log,) = _serving_log(capsys)
    assert log["answer"]["vendor_calls"] == {"cala": 2} and log["avoided"] is None
    # The admission candidate carries what the answer took.
    assert state["submitted"][0].answer_cost == COST


# --- stored with the entry, entities in the admission log -----------------


def test_memory_store_returns_the_answer_cost_with_a_search_row():
    store = MemoryStore()
    store.create_pending(make_candidate(answer_cost=COST), "s3://e")
    assert next(iter(store.rows.values()))["answer_cost"] == COST


def test_the_admission_log_names_the_entities(capsys):
    _admit()
    (log,) = [x for x in map(json.loads, capsys.readouterr().out.strip().splitlines()) if x["event"] == "cache.admission"]
    assert log["entities"] == CLASSIFICATION["entities"]
