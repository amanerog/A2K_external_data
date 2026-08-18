"""Calls the MCP tool functions the same way an MCP client would
(`mcp.call_tool`), checking the transport wiring rather than re-testing
gateway logic already covered in test_gateway_*.py.
"""

import json
from types import SimpleNamespace

from a2k.mcp_server import server
from a2k.mcp_server.server import engine, mcp


def _content_json(result):
    content, _structured = result if isinstance(result, tuple) else (result, None)
    return json.loads(content[0].text)


async def test_tools_and_resources_are_registered():
    tools = await mcp.list_tools()
    names = {t.name for t in tools}
    assert names == {
        "a2k.search",
        "a2k.ask",
        "a2k.listVendors",
        "a2k.explain",
        "a2k.getDocument",
        "a2k.validateCitation",
        "a2k.reportConflict",
        "a2k.getAuditRecord",
    }

    resources = await mcp.list_resources()
    uris = {str(r.uri) for r in resources}
    assert uris == {"a2k://card", "a2k://card/cala", "a2k://card/sayari"}


async def test_ask_tool_matches_engine_envelope_shape():
    result = await mcp.call_tool("a2k.ask", {"query": "Meridian Textiles ownership"})
    data = _content_json(result)
    assert data["ok"] is True
    assert len(data["conflicts"]) == 1
    # Level 2 (see README "Conformance"): no response signing.
    assert data["responseSignature"] is None


async def test_explain_tool_uses_prior_ask_request_id():
    ask_result = await mcp.call_tool("a2k.ask", {"query": "Acme Robotics"})
    ask_data = _content_json(ask_result)
    request_id = ask_data["audit"]["requestId"]

    explain_result = await mcp.call_tool("a2k.explain", {"answerRef": request_id})
    explain_data = _content_json(explain_result)
    assert explain_data["ok"] is True
    assert explain_data["operation"] == "explain"


async def test_validate_citation_tool_detects_mismatch():
    search_result = await mcp.call_tool("a2k.search", {"query": "Acme Robotics"})
    search_data = _content_json(search_result)
    citation = search_data["citations"][0]

    ok_result = await mcp.call_tool(
        "a2k.validateCitation", {"documentId": citation["documentId"], "exact": citation["selector"]["exact"]}
    )
    assert _content_json(ok_result)["valid"] is True

    bad_result = await mcp.call_tool(
        "a2k.validateCitation", {"documentId": citation["documentId"], "exact": "this text is not in the document"}
    )
    assert _content_json(bad_result)["valid"] is False


async def test_report_conflict_tool_returns_full_report():
    ask_result = await mcp.call_tool("a2k.ask", {"query": "Meridian Textiles ownership"})
    request_id = _content_json(ask_result)["audit"]["requestId"]

    report_result = await mcp.call_tool("a2k.reportConflict", {"answerRef": request_id})
    report_data = _content_json(report_result)
    assert report_data["ok"] is True
    assert report_data["conflictReport"]["reconciliation"]["status"] == "unresolved-surfaced"


async def test_list_vendors_tool_returns_domains_topics_and_scope():
    result = await mcp.call_tool("a2k.listVendors", {})
    data = _content_json(result)
    vendors = {v["sourceId"]: v for v in data["vendors"]}
    assert set(vendors) == {"cala", "sayari"}

    cala, sayari = vendors["cala"], vendors["sayari"]
    for vendor in (cala, sayari):
        assert vendor["domains"]
        assert vendor["topics"]
        assert vendor["scope"]

    # the whole point of granularizing the cards: no shared topic strings for
    # the agent to get confused by when picking a `sources` value.
    assert not set(cala["topics"]) & set(sayari["topics"])


async def test_cala_raw_mode_disabled_by_default(monkeypatch):
    """config.cala_raw_knowledge_search defaults False -- a2k.ask keeps its normal
    cited-envelope shape unless someone explicitly opts in."""
    called = False

    async def _fail_if_called(query):
        nonlocal called
        called = True
        return {"content": "should not be reached"}

    monkeypatch.setattr(engine.adapters["cala"], "raw_knowledge_search", _fail_if_called)

    result = await mcp.call_tool("a2k.ask", {"query": "Meridian Textiles ownership"})
    data = _content_json(result)
    assert called is False
    assert "ok" in data  # normal envelope shape, not the raw {"content": ...} shape


async def test_cala_raw_mode_returns_content_unprocessed_when_enabled(monkeypatch):
    monkeypatch.setattr(server, "config", SimpleNamespace(cala_raw_knowledge_search=True))

    async def _fake_raw_knowledge_search(query):
        return {"content": f"Cala's own synthesized answer about {query}", "explainability": ["ignored by this path"]}

    monkeypatch.setattr(engine.adapters["cala"], "raw_knowledge_search", _fake_raw_knowledge_search)

    result = await mcp.call_tool("a2k.ask", {"query": "Meridian Textiles"})
    data = _content_json(result)
    assert data == {"content": "Cala's own synthesized answer about Meridian Textiles", "explainability": ["ignored by this path"]}


async def test_cala_raw_mode_skipped_when_sources_excludes_cala(monkeypatch):
    monkeypatch.setattr(server, "config", SimpleNamespace(cala_raw_knowledge_search=True))
    called = False

    async def _fail_if_called(query):
        nonlocal called
        called = True
        return {}

    monkeypatch.setattr(engine.adapters["cala"], "raw_knowledge_search", _fail_if_called)

    result = await mcp.call_tool("a2k.search", {"query": "Meridian Textiles", "sources": ["sayari"]})
    data = _content_json(result)
    assert called is False
    assert "citations" in data  # normal a2k.search envelope shape
