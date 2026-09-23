"""Route-level tests for app.py. a2k_client's own new_session/call_ask/call_search
are monkeypatched here (patched on app_module, the names app.py actually calls --
patching a2k_client's copies would miss app.py's own already-bound references) --
see test_a2k_client.py for coverage of a2k_client.py's own MCP-call/SSE-parsing
logic, which these tests deliberately don't re-exercise.
"""

from __future__ import annotations

import app as app_module
import pytest
from a2k_client import A2KClientError
from fastapi.testclient import TestClient


@pytest.fixture
def client():
    return TestClient(app_module.app)


def test_health(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"service": "a2k-frontend", "status": "ok"}


def test_index_renders_html(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]


def test_api_ask_returns_envelope_from_a2k_client(client, monkeypatch):
    monkeypatch.setattr(app_module, "new_session", lambda c: "sess-1")
    monkeypatch.setattr(
        app_module, "call_ask", lambda c, session_id, query, sources: {"ok": True, "answer": "42", "citations": []}
    )
    resp = client.post("/api/ask", json={"query": "who owns X?"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "answer": "42", "citations": []}


def test_api_ask_passes_query_and_sources_through(client, monkeypatch):
    captured = {}

    def fake_call_ask(c, session_id, query, sources):
        captured["session_id"] = session_id
        captured["query"] = query
        captured["sources"] = sources
        return {"ok": True}

    monkeypatch.setattr(app_module, "new_session", lambda c: "sess-42")
    monkeypatch.setattr(app_module, "call_ask", fake_call_ask)

    client.post("/api/ask", json={"query": "who owns X?", "sources": ["cala"]})

    assert captured == {"session_id": "sess-42", "query": "who owns X?", "sources": ["cala"]}


def test_api_ask_empty_sources_list_becomes_none(client, monkeypatch):
    """`sources: []` from the browser (nothing checked) means "let the agent
    decide", same as omitting it entirely on a raw MCP call -- app.py's own
    `req.sources or None` is what does this collapsing; confirm it actually
    reaches a2k_client as None, not as an empty list."""
    captured = {}
    monkeypatch.setattr(app_module, "new_session", lambda c: "sess-1")
    monkeypatch.setattr(app_module, "call_ask", lambda c, session_id, query, sources: captured.setdefault("sources", sources) or {"ok": True})

    client.post("/api/ask", json={"query": "q", "sources": []})

    assert captured["sources"] is None


def test_api_ask_transport_error_returns_502(client, monkeypatch):
    def raise_transport_error(c):
        raise A2KClientError("no JSON-RPC response with id=1 in AgentCore response body")

    monkeypatch.setattr(app_module, "new_session", raise_transport_error)

    resp = client.post("/api/ask", json={"query": "q"})

    assert resp.status_code == 502
    body = resp.json()
    assert body["ok"] is False
    assert body["error"]["code"] == "TRANSPORT_ERROR"
    assert "no JSON-RPC response" in body["error"]["message"]


def test_api_search_returns_envelope_from_a2k_client(client, monkeypatch):
    monkeypatch.setattr(app_module, "new_session", lambda c: "sess-1")
    monkeypatch.setattr(
        app_module, "call_search", lambda c, session_id, query, sources: {"ok": True, "passages": [], "citations": []}
    )
    resp = client.post("/api/search", json={"query": "q"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "passages": [], "citations": []}


def test_api_search_transport_error_returns_502(client, monkeypatch):
    monkeypatch.setattr(app_module, "new_session", lambda c: "sess-1")

    def raise_transport_error(c, session_id, query, sources):
        raise A2KClientError("boom")

    monkeypatch.setattr(app_module, "call_search", raise_transport_error)

    resp = client.post("/api/search", json={"query": "q"})

    assert resp.status_code == 502
    assert resp.json()["ok"] is False
