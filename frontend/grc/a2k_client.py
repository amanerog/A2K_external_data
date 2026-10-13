"""Direct MCP client for a2k-box's deployed AgentCore Runtime -- the same
`invoke_agent_runtime` + JSON-RPC/SSE pattern every audit/test script in
`deploy/agentcore/` already uses (see e.g. `run_vendor_audit.py`'s
`_new_mcp_session()`/`_extract_jsonrpc_result()`), ported here rather than
re-derived so this frontend calls the MCP exactly the way it's already
proven to work.

Auth: IAM/SigV4, via whatever credentials `boto3`'s default chain finds --
the developer's own AWS CLI/SSO session locally, an EKS pod's IRSA role once
this is deployed there. No bearer token, no separate login flow needed.

Deliberately opens a fresh MCP session (`initialize`) per call rather than
pooling/reusing sessions across requests -- see this feature's plan for why:
each web request is independent and short-lived, and session reuse across
concurrent users would need session-affinity/isolation this doesn't need
yet.
"""

from __future__ import annotations

import json
import os
from typing import Any, Optional

REGION = os.environ.get("AWS_REGION", "eu-west-1")
# The a2k-box MCP Runtime this frontend talks to: a2k_cache_mcp, the one with
# the Aurora answer cache and the current agent (stack a2k-cache-runtimes).
# Pointing this at another Runtime silently "works" -- real answers still
# come back -- without the cache that Runtime lacks (confirmed live
# 2026-09-22 with the earlier DynamoDB-cache Runtime). Overridable so this
# needs no code change if a2k-box is redeployed under another Runtime.
AGENT_RUNTIME_ARN = os.environ.get(
    "A2K_BOX_RUNTIME_ARN",
    "arn:aws:bedrock-agentcore:eu-west-1:396961015428:runtime/a2k_cache_mcp-48ImkT2F9a",
)
# Since the A2K 0.1 migration (2026-10), a2k.ask/a2k.search take the 0.1
# request and return the 0.1 envelope -- the surface K2 uses. This page
# renders a2k-box's own CitedResponseEnvelope (audit.decisionReason for the
# cache badge, freshness, toolCalls, usage), which the same Runtime still
# serves as internal.ask/internal.search when A2K_INTERNAL_TOOLS=true (test
# Runtimes only -- the dev Runtime has it).
ASK_TOOL = os.environ.get("A2K_ASK_TOOL", "internal.ask")
SEARCH_TOOL = os.environ.get("A2K_SEARCH_TOOL", "internal.search")
MCP_PROTOCOL_VERSION = "2025-06-18"


class A2KClientError(Exception):
    """Raised for anything that goes wrong talking to the MCP itself (auth,
    network, malformed response) -- distinct from a2k-box returning a
    well-formed envelope with `ok: false` (that's a normal response, not an
    exception; see app.py)."""


def new_session(client) -> str:
    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier="DEFAULT",
        payload=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "a2k-frontend", "version": "1.0"},
                },
            }
        ).encode("utf-8"),
        contentType="application/json",
        accept="application/json, text/event-stream",
        mcpProtocolVersion=MCP_PROTOCOL_VERSION,
    )
    response["response"].read()  # drain -- only the session id matters here
    session_id = response.get("mcpSessionId")
    if not session_id:
        raise A2KClientError("initialize response carried no mcpSessionId")
    return session_id


def _extract_jsonrpc_result(body: str, expected_id: int) -> dict:
    """AgentCore streams SSE (`event: ...` / `data: ...` lines, plus bare
    `: ping - <timestamp>` comment lines while the tool call is still
    running) -- the one line that matters is the `data: ` line whose
    JSON-RPC `id` matches the request we sent. Same parsing every
    deploy/agentcore/ script already relies on."""
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            obj = json.loads(line[len("data: "):])
        except json.JSONDecodeError:
            continue
        if obj.get("id") == expected_id:
            return obj
    raise A2KClientError(f"no JSON-RPC response with id={expected_id} in AgentCore response body")


def _call_tool(client, session_id: str, tool_name: str, query: str, sources: Optional[list[str]]) -> dict[str, Any]:
    arguments: dict[str, Any] = {"query": query}
    if sources:
        arguments["sources"] = sources

    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier="DEFAULT",
        payload=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }
        ).encode("utf-8"),
        contentType="application/json",
        accept="application/json, text/event-stream",
        mcpProtocolVersion=MCP_PROTOCOL_VERSION,
        mcpSessionId=session_id,
    )
    status_code = response["statusCode"]
    body = response["response"].read().decode("utf-8")
    if status_code != 200:
        raise A2KClientError(f"invoke_agent_runtime statusCode={status_code}: {body[:500]}")

    rpc_result = _extract_jsonrpc_result(body, expected_id=2)
    if "error" in rpc_result:
        raise A2KClientError(f"MCP error: {rpc_result['error']}")
    result = rpc_result["result"]
    if result.get("isError"):
        raise A2KClientError(f"{tool_name} tool error: {result}")

    text = result["content"][0]["text"]
    return json.loads(text)


def call_ask(client, session_id: str, query: str, sources: Optional[list[str]] = None) -> dict[str, Any]:
    """Returns the parsed CitedResponseEnvelope dict (a2k/models/envelope.py's
    shape) -- `answer`/`claims`/`citations`/`grounding`/`freshness`/`audit`/
    `conflicts`/`usage`/`toolCalls`. Raises A2KClientError if the call itself
    failed; a well-formed `{"ok": false, "error": {...}}` envelope is
    returned normally, not raised (that's a2k-box's own answer, not a
    transport failure)."""
    return _call_tool(client, session_id, ASK_TOOL, query, sources)


def call_search(client, session_id: str, query: str, sources: Optional[list[str]] = None) -> dict[str, Any]:
    return _call_tool(client, session_id, SEARCH_TOOL, query, sources)
