"""Unit tests for the model-free search path (direct_agent.SEARCH_TOOLS,
run_vendor_search_sync): after phase 1 picks the vendor, one call to its
search tool, results mapped onto passages/citations in code. Network-free:
the vendor MCP client is faked, and `sources` is passed so phase 1 never runs.

    cd agent && python -m pytest test_direct_agent_search.py -v
"""

import asyncio
import json

import pytest

import direct_agent as da
import vendor_mcp_client

CATALOGUE = [{"sourceId": s, "status": "active"} for s in ("cala", "linkup", "sayari")]

CALA_OUTPUT = {
    "content": "## Cala's own written summary -- must not become a passage",
    "context": [
        {
            "id": "c1",
            "content": "98. Una cultura de riesgos sólida debería incluir, entre otros, los siguientes elementos...",
            "origins": [
                {"source": {"name": "bde.es", "url": "https://www.bde.es/guia.pdf"},
                 "document": {"name": "Directrices EBA Gobierno Interno", "url": "https://www.bde.es/guia.pdf"}}
            ],
        },
        {"id": "c2", "content": "Otro extracto del mismo documento.", "origins": [{"document": {"name": "x", "url": "https://www.bde.es/guia.pdf"}}]},
        {"id": "c3", "content": "", "origins": []},
    ],
}
LINKUP_OUTPUT = {"results": [
    {"name": "EBA GL 2021/05", "url": "https://www.eba.europa.eu/gl.pdf", "content": "evaluación de la idoneidad..."},
    {"name": "no url", "url": None, "content": "dropped"},
]}
SAYARI_OUTPUT = {"items": [{"entity_id": "abc", "label": "Banco Santander Totta", "type": "company", "country": "PRT"}]}


class FakeClient:
    def __init__(self, output, status="success"):
        self.output, self.status, self.calls = output, status, []

    def call_tool_sync(self, *, tool_use_id, name, arguments):
        self.calls.append((name, arguments))
        return {"status": self.status, "toolUseId": tool_use_id, "content": [{"text": json.dumps(self.output)}]}

    def stop(self, *a):
        pass


@pytest.fixture
def vendor(monkeypatch):
    def set_(output, status="success"):
        client = FakeClient(output, status)
        monkeypatch.setattr(vendor_mcp_client, "connect_vendor", lambda source_id: (client, []))
        return client

    return set_


def _search(source):
    return asyncio.run(da.handle(operation="search", query="EBA cultura de riesgos", sources=[source], catalogue=CATALOGUE, model_id="m"))


def test_cala_search_returns_the_origin_excerpts_not_calas_summary(vendor):
    client = vendor(CALA_OUTPUT)
    out = _search("cala")
    assert client.calls == [("knowledge_search", {"input": "EBA cultura de riesgos"})]
    assert [p["text"][:3] for p in out["passages"]] == ["98.", "Otr"]
    assert all("summary" not in p["text"] for p in out["passages"])
    assert out["citations"] == [{"title": "Directrices EBA Gobierno Interno", "sourceUrl": "https://www.bde.es/guia.pdf", "documentId": "https://www.bde.es/guia.pdf"}]
    assert [p["citationIndexes"] for p in out["passages"]] == [[0], [0]]  # same document, one citation
    assert out["usage"]["totalTokens"] == 0 and out["toolCalls"][0]["toolName"] == "knowledge_search"


def test_linkup_search_maps_each_result_with_a_url(vendor):
    vendor(LINKUP_OUTPUT)
    out = _search("linkup")
    assert [c["sourceUrl"] for c in out["citations"]] == ["https://www.eba.europa.eu/gl.pdf"]
    assert out["passages"] == [{"text": "evaluación de la idoneidad...", "citationIndexes": [0]}]


def test_sayari_entities_are_passages_without_a_url(vendor):
    vendor(SAYARI_OUTPUT)
    out = _search("sayari")
    assert out["passages"][0]["text"] == "Banco Santander Totta -- company -- PRT"
    assert out["citations"][0]["sourceUrl"] is None and out["citations"][0]["documentId"] == "sayari:abc"


def test_a_failed_search_call_drops_the_vendor(vendor):
    vendor({"error": "boom"}, status="error")
    out = _search("cala")
    assert out["passages"] == [] and out["citations"] == [] and out["vendorUnavailable"] is True
