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
        app_module, "call_ask", lambda c, session_id, query, sources, user=None: {"ok": True, "answer": "42", "citations": []}
    )
    resp = client.post("/api/ask", json={"query": "who owns X?"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "answer": "42", "citations": []}


def test_api_ask_passes_query_and_sources_through(client, monkeypatch):
    captured = {}

    def fake_call_ask(c, session_id, query, sources, user=None):
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
    monkeypatch.setattr(app_module, "call_ask", lambda c, session_id, query, sources, user=None: captured.setdefault("sources", sources) or {"ok": True})

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
        app_module, "call_search", lambda c, session_id, query, sources, user=None: {"ok": True, "passages": [], "citations": []}
    )
    resp = client.post("/api/search", json={"query": "q"})
    assert resp.status_code == 200
    assert resp.json() == {"ok": True, "passages": [], "citations": []}


def test_api_search_transport_error_returns_502(client, monkeypatch):
    monkeypatch.setattr(app_module, "new_session", lambda c: "sess-1")

    def raise_transport_error(c, session_id, query, sources, user=None):
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


# --- per-user HTTP Basic auth and the request trail --------------------------

from passwords import hash_password, verify_password  # noqa: E402

REAL_READ_USERS = app_module._read_users


@pytest.fixture(autouse=True)
def _fresh_users(monkeypatch):
    """No users configured unless a test says so; nothing cached across tests."""
    monkeypatch.setattr(app_module, "_users", {"loaded_at": None, "hashes": {}, "verified": set()})
    monkeypatch.setattr(app_module, "_read_users", lambda: {})


@pytest.fixture
def protected(monkeypatch):
    hashes = {"alice": hash_password("pw-alice", iterations=1000), "bob": hash_password("pw-bob", iterations=1000)}
    monkeypatch.setattr(app_module, "_read_users", lambda: hashes)
    return TestClient(app_module.app)


def test_without_users_everything_is_open(client):
    assert client.get("/").status_code == 200


def test_each_user_gets_in_with_their_own_password(protected):
    # Basic auth stays, for scripts (curl -u); browsers go through /login.
    resp = protected.get("/", follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    assert protected.post("/api/ask", json={"query": "q"}).json()["error"]["code"] == "UNAUTHENTICATED"
    assert protected.get("/", auth=("alice", "pw-alice")).status_code == 200
    assert protected.get("/", auth=("bob", "pw-bob")).status_code == 200
    assert protected.get("/", auth=("alice", "pw-bob"), follow_redirects=False).status_code == 303
    assert protected.get("/", auth=("carol", "pw-alice"), follow_redirects=False).status_code == 303


def test_health_stays_open(protected):
    assert protected.get("/health").status_code == 200


def test_the_question_carries_the_user_and_leaves_a_trail(protected, monkeypatch, capsys):
    seen = {}

    def fake_call_ask(c, session_id, query, sources, user=None):
        seen["user"] = user
        return {"ok": True, "answer": "42", "audit": {"requestId": "req-1", "decisionReason": "served from cache entry e1"}}

    monkeypatch.setattr(app_module, "new_session", lambda c: "sess-1")
    monkeypatch.setattr(app_module, "call_ask", fake_call_ask)
    resp = protected.post("/api/ask", json={"query": "¿Qué dice la EBA?"}, auth=("alice", "pw-alice"))
    assert resp.status_code == 200 and seen["user"] == "alice"
    import hashlib, json as _json

    (line,) = [_json.loads(x) for x in capsys.readouterr().out.splitlines() if '"frontend.request"' in x]
    assert line["user"] == "alice" and line["request_id"] == "req-1" and line["served_from_cache"] is True
    assert line["query_sha256"] == hashlib.sha256("¿Qué dice la EBA?".encode()).hexdigest()[:16]


def test_a_failed_login_is_logged_without_the_password(protected, capsys):
    protected.get("/", auth=("alice", "wrong-pw"))
    out = capsys.readouterr().out
    assert '"frontend.auth_failed"' in out and "wrong-pw" not in out


def test_users_are_reloaded_after_the_ttl(monkeypatch):
    calls = []
    monkeypatch.setattr(app_module, "_read_users", lambda: calls.append(1) or {"alice": hash_password("x", iterations=1000)})
    monkeypatch.setattr(app_module, "USERS_TTL", 300.0)
    app_module._current_users(); app_module._current_users()
    assert len(calls) == 1
    app_module._users["loaded_at"] -= 301
    app_module._current_users()
    assert len(calls) == 2


def test_a_legacy_single_password_secret_is_user_a2k(monkeypatch):
    monkeypatch.delenv("A2K_FRONTEND_PASSWORD_SECRET_ARN", raising=False)
    monkeypatch.setenv("A2K_FRONTEND_PASSWORD", "shared-pw")
    users = REAL_READ_USERS()
    assert list(users) == ["a2k"] and verify_password("shared-pw", users["a2k"])


def test_users_secret_format_is_read(monkeypatch):
    import json

    monkeypatch.delenv("A2K_FRONTEND_PASSWORD_SECRET_ARN", raising=False)
    monkeypatch.setenv("A2K_FRONTEND_PASSWORD", json.dumps({"users": {"alice": hash_password("pw", iterations=1000)}}))
    users = REAL_READ_USERS()
    assert list(users) == ["alice"] and verify_password("pw", users["alice"])


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


# --- sign-in page and session cookie (Function URLs strip WWW-Authenticate) ---


def test_login_sets_a_session_that_opens_the_page(protected):
    assert protected.get("/login").status_code == 200
    bad = protected.post("/login", data={"user": "alice", "password": "nope"}, follow_redirects=False)
    assert bad.status_code == 401 and "incorrectos" in bad.text
    ok = protected.post("/login", data={"user": "alice", "password": "pw-alice"}, follow_redirects=False)
    assert ok.status_code == 303 and ok.headers["location"] == "/"
    assert "httponly" in ok.headers["set-cookie"].lower() and "samesite=lax" in ok.headers["set-cookie"].lower()
    page = protected.get("/")
    assert page.status_code == 200 and "alice" in page.text and "/logout" in page.text


def test_the_session_ends_on_logout_and_on_a_password_reset(protected, monkeypatch):
    protected.post("/login", data={"user": "alice", "password": "pw-alice"})
    assert protected.get("/", follow_redirects=False).status_code == 200
    protected.get("/logout")
    assert protected.get("/", follow_redirects=False).status_code == 303
    protected.post("/login", data={"user": "alice", "password": "pw-alice"})
    new = {"alice": hash_password("other", iterations=1000), "bob": hash_password("pw-bob", iterations=1000)}
    monkeypatch.setattr(app_module, "_users", {"loaded_at": None, "hashes": {}, "verified": set()})
    monkeypatch.setattr(app_module, "_read_users", lambda: new)
    assert protected.get("/", follow_redirects=False).status_code == 303  # reset: old session refused


def test_a_forged_or_expired_cookie_is_refused(protected):
    hashes = app_module._current_users()
    good = app_module._session_cookie("alice", hashes["alice"])
    name, expires, sig = good.split(".")
    assert app_module._session_user(good, hashes) == "alice"
    assert app_module._session_user(f"{name}.{int(expires) + 999}.{sig}", hashes) is None
    assert app_module._session_user(f"{name}.{expires}.{'0' * 64}", hashes) is None
    assert app_module._session_user("garbage", hashes) is None
    assert app_module._session_user(f"{name}.1.{app_module._session_signature('alice', 1, hashes['alice'])}", hashes) is None


def test_static_files_and_login_are_open(protected):
    assert protected.get("/static/style.css").status_code == 200
    assert protected.get("/login").status_code == 200


def test_a_user_added_after_the_list_was_loaded_can_sign_in_at_once(monkeypatch):
    lists = [{"alice": hash_password("pw-alice", iterations=1000)}]
    monkeypatch.setattr(app_module, "_read_users", lambda: lists[-1])
    client = TestClient(app_module.app)
    assert client.get("/", follow_redirects=False).status_code == 303  # list loaded: alice only
    lists.append({**lists[0], "carol": hash_password("pw-carol", iterations=1000)})
    app_module._users["loaded_at"] -= 11  # more than 10 s ago, well within the 5-minute TTL
    resp = client.post("/login", data={"user": "carol", "password": "pw-carol"}, follow_redirects=False)
    assert resp.status_code == 303 and resp.headers["location"] == "/"
