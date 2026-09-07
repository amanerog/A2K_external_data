"""Direct connections to Cala's and Sayari's own hosted MCP servers, opened
from the agent's own process -- bypasses a2k-box entirely. Part of this
branch's architecture (mcp_to_agent_to_mcp): the agent discovers each
vendor's tools live via `tools/list` and lets the model decide what to call,
rather than going through a2k-box's deterministic adapters
(a2k/adapters/cala_mcp.py, a2k/adapters/sayari_mcp.py).

Mirrors those adapters' own URLs/auth exactly (same vendor endpoints,
same credentials) -- duplicated here rather than imported, since the
agent's deploy zip doesn't bundle the `a2k` package at all (see
agent/README.md's "Files in this directory": only core.py/entrypoint*.py/
observability.py get copied in). Wrapped as Strands `MCPClient`/
`MCPAgentTool` objects (like core.py's `_get_tools_and_catalogue` does for
the Gateway connection) rather than raw `mcp.ClientSession` like the a2k-box
adapters use, since the agent needs Strands-compatible tools to hand to
`Agent(tools=...)`, not a hand-rolled call loop.

Deliberately synchronous (`start()`/`list_tools_sync()`, plain `httpx.post`
for the Sayari token fetch), matching core.py's `_get_tools_and_catalogue`/
`get_bearer_token` -- Strands' `MCPClient` manages its own background
thread/event loop internally, so sync calls here are fine even though the
caller (direct_agent.py) is itself async.
"""

from __future__ import annotations

import os
import time

import httpx
from mcp.client.streamable_http import streamablehttp_client
from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

from core import secret_env

CALA_MCP_URL = os.environ.get("CALA_MCP_URL", "https://api.cala.ai/mcp/")
SAYARI_MCP_URL = os.environ.get("SAYARI_MCP_URL", "https://mcp.sayari.com/mcp")

# Same Auth0 tenant/audience as a2k/adapters/sayari_mcp.py -- Sayari's MCP
# server uses a *separate* credential/grant from its REST API (confirmed
# live 2026-08-12, see that adapter's own module docstring for the
# troubleshooting history behind this).
AUTH0_TOKEN_URL = "https://sayari.auth0.com/oauth/token"
AUTH0_AUDIENCE = "https://mcp.sayari.com/"

# (token, expiry epoch seconds) keyed by client_id -- same caching shape as
# core.py's own _token_cache for the Gateway's Cognito token, just for
# Sayari's Auth0 token instead. No lock here (unlike core.py's _cache_lock):
# each vendor connection is opened fresh per direct_agent.py call rather than
# cached/reused across concurrent requests, so there's no concurrent-writer
# race to guard against the way there is for the Gateway's long-lived cache.
_sayari_token_cache: dict[str, tuple[str, float]] = {}
_SAYARI_TOKEN_EXPIRY_MARGIN_SECONDS = 30


def _get_sayari_token(client_id: str, client_secret: str) -> str:
    cached = _sayari_token_cache.get(client_id)
    if cached and cached[1] > time.time():
        return cached[0]

    response = httpx.post(
        AUTH0_TOKEN_URL,
        json={
            "client_id": client_id,
            "client_secret": client_secret,
            "audience": AUTH0_AUDIENCE,
            "grant_type": "client_credentials",
        },
        headers={"accept": "application/json"},
        timeout=30,
    )
    response.raise_for_status()
    payload = response.json()
    token = payload["access_token"]
    expires_in = float(payload.get("expires_in", 86400))
    _sayari_token_cache[client_id] = (token, time.time() + expires_in - _SAYARI_TOKEN_EXPIRY_MARGIN_SECONDS)
    return token


def _rename_tools_for_model(tools: list) -> list[MCPAgentTool]:
    """Bedrock's Converse API restricts tool names to [a-zA-Z0-9_-]+. Both
    vendors' own tool names are already clean (search_entities,
    get_entity_summary, entity_search, knowledge_query, ...) but this is
    applied unconditionally rather than assumed -- same reasoning as core.py's
    _get_tools_and_catalogue for the Gateway's dotted a2k.* names, and cheap
    insurance against a future vendor tool name that isn't clean."""
    return [
        MCPAgentTool(tool.mcp_tool, tool.mcp_client, name_override=tool.tool_name.replace(".", "_"))
        for tool in tools
    ]


def connect_cala() -> tuple[MCPClient, list[MCPAgentTool]]:
    """Opens a direct connection to Cala's own MCP server (X-API-KEY auth,
    same credential as a2k-box's REST/MCP adapters). Caller owns the
    returned MCPClient's lifecycle -- already `.start()`ed here; call
    `.stop(None, None, None)` when done. Unlike core.py's Gateway connection
    (cached across calls, since it's used on every single request), this is
    opened fresh per call: direct_agent.py only opens a vendor connection
    once it has already decided that vendor is needed for this one question."""
    api_key = secret_env("CALA_API_KEY")
    headers = {"X-API-KEY": api_key}

    def _transport():
        return streamablehttp_client(CALA_MCP_URL, headers=headers, timeout=120)

    mcp_client = MCPClient(_transport)
    mcp_client.start()
    tools = mcp_client.list_tools_sync()
    return mcp_client, _rename_tools_for_model(tools)


def connect_sayari() -> tuple[MCPClient, list[MCPAgentTool]]:
    """Opens a direct connection to Sayari's own MCP server (Auth0
    client-credentials -- see module docstring for why this is a separate
    credential from Sayari's REST API). Same lifecycle contract as
    connect_cala() above."""
    client_id = secret_env("AUTH0_CLIENT_ID")
    client_secret = secret_env("AUTH0_CLIENT_SECRET")
    token = _get_sayari_token(client_id, client_secret)
    headers = {"Authorization": f"Bearer {token}"}

    def _transport():
        return streamablehttp_client(SAYARI_MCP_URL, headers=headers, timeout=120)

    mcp_client = MCPClient(_transport)
    mcp_client.start()
    tools = mcp_client.list_tools_sync()
    return mcp_client, _rename_tools_for_model(tools)


VENDOR_CONNECTORS = {
    "cala": connect_cala,
    "sayari": connect_sayari,
}


def connect_vendor(source_id: str) -> tuple[MCPClient, list[MCPAgentTool]]:
    """Dispatches to connect_cala()/connect_sayari() by KB Card sourceId --
    the single entry point direct_agent.py's phase 2 uses once phase 1 has
    already decided which vendor(s) this question needs."""
    connector = VENDOR_CONNECTORS.get(source_id)
    if connector is None:
        raise ValueError(f"No direct MCP connector for vendor {source_id!r}")
    return connector()


def disconnect(mcp_client: MCPClient) -> None:
    """Best-effort close -- mirrors core.py's _evict_mcp_cache's own
    best-effort .stop() call; a connection that's already broken shouldn't
    raise on cleanup."""
    try:
        mcp_client.stop(None, None, None)
    except Exception:
        pass
