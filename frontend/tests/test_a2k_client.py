"""Unit tests for a2k_client.py's session/SSE-parsing logic, against a fake
boto3 bedrock-agentcore client -- no real AWS call, no network. Covers the
same JSON-RPC/SSE shape every audit script in ../../deploy/agentcore/ relies
on (see a2k_client.py's own module docstring): `initialize` first (session
id out), then `tools/call` (SSE body in, JSON-RPC `data:` line with a
matching `id` picked out, `result.content[0].text` JSON-parsed as the
envelope).
"""

from __future__ import annotations

import json

import a2k_client
import pytest


class _FakeBody:
    """Stands in for the streaming body `invoke_agent_runtime()["response"]`
    returns -- a2k_client.py only ever calls `.read()` on it."""

    def __init__(self, text: str):
        self._bytes = text.encode("utf-8")

    def read(self) -> bytes:
        return self._bytes


class _FakeAgentCoreClient:
    """Branches on the JSON-RPC `method` in the outgoing payload -- same two
    calls a2k_client.py ever makes (`initialize`, then `tools/call`) -- so one
    fake client can serve both `new_session()` and `_call_tool()` without
    needing a call counter."""

    def __init__(self, *, session_id: str = "sess-abc", tool_response_body: str = "", tool_status_code: int = 200, omit_session_id: bool = False):
        self._session_id = session_id
        self._tool_response_body = tool_response_body
        self._tool_status_code = tool_status_code
        self._omit_session_id = omit_session_id
        self.calls: list[dict] = []

    def invoke_agent_runtime(self, **kwargs):
        self.calls.append(kwargs)
        payload = json.loads(kwargs["payload"])
        if payload["method"] == "initialize":
            response: dict = {"response": _FakeBody(""), "statusCode": 200}
            if not self._omit_session_id:
                response["mcpSessionId"] = self._session_id
            return response
        return {"response": _FakeBody(self._tool_response_body), "statusCode": self._tool_status_code}


def _sse(obj: dict) -> str:
    return f"data: {json.dumps(obj)}\n\n"


def _envelope_rpc_body(envelope: dict, *, request_id: int = 2) -> str:
    return _sse({"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"text": json.dumps(envelope)}]}})


# --- new_session() -----------------------------------------------------------


def test_new_session_returns_session_id():
    client = _FakeAgentCoreClient(session_id="sess-123")
    assert a2k_client.new_session(client) == "sess-123"


def test_new_session_raises_when_session_id_missing():
    client = _FakeAgentCoreClient(omit_session_id=True)
    with pytest.raises(a2k_client.A2KClientError, match="mcpSessionId"):
        a2k_client.new_session(client)


# --- _extract_jsonrpc_result() ------------------------------------------------


def test_extract_jsonrpc_result_skips_ping_comment_lines():
    body = ": ping - 2026-09-23T00:00:00Z\n" + _sse({"jsonrpc": "2.0", "id": 2, "result": {"ok": True}})
    result = a2k_client._extract_jsonrpc_result(body, expected_id=2)
    assert result["result"] == {"ok": True}


def test_extract_jsonrpc_result_raises_when_no_matching_id():
    body = _sse({"jsonrpc": "2.0", "id": 99, "result": {}})
    with pytest.raises(a2k_client.A2KClientError, match="id=2"):
        a2k_client._extract_jsonrpc_result(body, expected_id=2)


def test_extract_jsonrpc_result_skips_malformed_data_lines():
    """A `data: ` line that isn't valid JSON (seen alongside AgentCore's own
    `: ping - <timestamp>` comment lines while a tool call is still running)
    must be skipped, not raised on -- the real response line can still be
    further down in the same SSE stream."""
    body = "data: {not valid json\n" + _sse({"jsonrpc": "2.0", "id": 2, "result": {"ok": True}})
    result = a2k_client._extract_jsonrpc_result(body, expected_id=2)
    assert result["result"] == {"ok": True}


# --- call_ask() / call_search() (via _call_tool()) ---------------------------


def test_call_ask_returns_parsed_envelope():
    envelope = {"ok": True, "answer": "42", "citations": []}
    client = _FakeAgentCoreClient(tool_response_body=_envelope_rpc_body(envelope))
    assert a2k_client.call_ask(client, "sess-1", "who owns X?") == envelope


def test_call_search_returns_parsed_envelope():
    envelope = {"ok": True, "passages": [], "citations": []}
    client = _FakeAgentCoreClient(tool_response_body=_envelope_rpc_body(envelope))
    assert a2k_client.call_search(client, "sess-1", "who owns X?") == envelope


def test_call_tool_sends_query_and_omits_sources_when_not_given():
    client = _FakeAgentCoreClient(tool_response_body=_envelope_rpc_body({"ok": True}))
    a2k_client.call_ask(client, "sess-1", "who owns X?")
    tool_call_payload = json.loads(client.calls[-1]["payload"])
    assert tool_call_payload["params"]["arguments"] == {"query": "who owns X?"}


def test_call_tool_includes_sources_when_given():
    client = _FakeAgentCoreClient(tool_response_body=_envelope_rpc_body({"ok": True}))
    a2k_client.call_ask(client, "sess-1", "who owns X?", sources=["cala", "sayari"])
    tool_call_payload = json.loads(client.calls[-1]["payload"])
    assert tool_call_payload["params"]["arguments"]["sources"] == ["cala", "sayari"]


def test_call_tool_raises_on_non_200_status_code():
    client = _FakeAgentCoreClient(tool_response_body="", tool_status_code=500)
    with pytest.raises(a2k_client.A2KClientError, match="statusCode=500"):
        a2k_client.call_ask(client, "sess-1", "q")


def test_call_tool_raises_on_jsonrpc_error():
    body = _sse({"jsonrpc": "2.0", "id": 2, "error": {"code": -32000, "message": "boom"}})
    client = _FakeAgentCoreClient(tool_response_body=body)
    with pytest.raises(a2k_client.A2KClientError, match="MCP error"):
        a2k_client.call_ask(client, "sess-1", "q")


def test_call_tool_raises_when_tool_result_is_error():
    body = _sse({"jsonrpc": "2.0", "id": 2, "result": {"isError": True, "content": [{"text": "boom"}]}})
    client = _FakeAgentCoreClient(tool_response_body=body)
    with pytest.raises(a2k_client.A2KClientError, match="tool error"):
        a2k_client.call_ask(client, "sess-1", "q")
