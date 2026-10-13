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


def test_index_prefixes_its_urls_with_the_base_path(client, monkeypatch):
    """Behind an ingress that publishes the app under a sub-path (and strips
    it), the browser must ask for static files and the API under that path."""
    import app as app_module

    monkeypatch.setattr(app_module, "BASE_PATH", "/a2kfrontexternaldata")
    html = client.get("/").text
    assert 'href="/a2kfrontexternaldata/static/style.css"' in html
    assert 'src="/a2kfrontexternaldata/static/app.js"' in html
    assert 'window.A2K_BASE_PATH = "/a2kfrontexternaldata"' in html


def test_index_without_a_base_path_keeps_root_urls(client):
    html = client.get("/").text
    assert 'href="/static/style.css"' in html and 'window.A2K_BASE_PATH = ""' in html


# --- HTTP Basic auth (on when a password is configured) ---------------------


@pytest.fixture
def protected(monkeypatch):
    monkeypatch.setattr(app_module, "_password", lambda: "s3cret")
    return TestClient(app_module.app)


def test_without_a_password_everything_is_open(client, monkeypatch):
    monkeypatch.setattr(app_module, "_password", lambda: None)
    assert client.get("/").status_code == 200


def test_with_a_password_the_page_and_api_need_it(protected):
    resp = protected.get("/")
    assert resp.status_code == 401 and resp.headers["www-authenticate"].startswith("Basic")
    assert protected.post("/api/ask", json={"query": "q"}).status_code == 401
    assert protected.get("/", auth=("a2k", "wrong")).status_code == 401
    assert protected.get("/", auth=("someone", "s3cret")).status_code == 401
    assert protected.get("/", auth=("a2k", "s3cret")).status_code == 200


def test_health_stays_open(protected):
    assert protected.get("/health").status_code == 200


def test_lambda_handler_serves_a_function_url_event(monkeypatch):
    """The Lambda entry point answers a Function URL (payload 2.0) event."""
    pytest.importorskip("mangum")
    import lambda_handler

    event = {
        "version": "2.0", "routeKey": "$default", "rawPath": "/health", "rawQueryString": "",
        "headers": {"host": "abc.lambda-url.eu-west-1.on.aws"},
        "requestContext": {"http": {"method": "GET", "path": "/health", "sourceIp": "1.2.3.4", "protocol": "HTTP/1.1"},
                           "domainName": "abc.lambda-url.eu-west-1.on.aws", "stage": "$default"},
        "isBase64Encoded": False,
    }
    resp = lambda_handler.handler(event, None)
    assert resp["statusCode"] == 200 and '"status":"ok"' in resp["body"]
