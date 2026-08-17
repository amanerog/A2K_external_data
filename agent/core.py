"""Cala/Sayari router agent -- shared by router_agent.py (local CLI) and
entrypoint.py (AgentCore Runtime deploy) so both stay in sync.

Answers company-intelligence questions by calling a2k-box's tools through
the AgentCore Gateway (../deploy/agentcore/README.md section 4), routing
between Cala and Sayari via the `sources` param a2k.ask already exposes
(a2k/mcp_server/server.py) -- this doesn't add a new mechanism, it just
steers the existing tool.

Routing rule for now (placeholder -- swap for something more principled
once real usage data exists): national queries -> Cala, international/
cross-border queries -> Sayari, ambiguous queries -> both (omit `sources`,
a2k.ask's own default fan-out). The decision is left to the model's own
reasoning via the system prompt below, not hardcoded classification code --
keeps this agent dumb/stateless and easy to re-tune by editing the prompt.

Auth: Cognito client_credentials flow (this Gateway's inbound identity --
domain/scope confirmed live 2026-08-17 against the my-user-pool-278is5ma
pool's "gateway-mcp-sayari-cala" resource server, see
../deploy/agentcore/test_gateway_mcp.py's docstring for how those were
found. Re-derive from the Cognito console if the gateway is ever recreated).
"""

from __future__ import annotations

import os

import httpx
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

COGNITO_TOKEN_URL = "https://my-domain-f9bf0du3.auth.eu-west-1.amazoncognito.com/oauth2/token"
SCOPE = "gateway-mcp-sayari-cala/genesis-gateway:invoke"

SYSTEM_PROMPT = """You are a company-intelligence assistant. Your tools query \
two source providers behind a single gateway: Cala (financial/legal/regulatory \
filings -- strongest for domestic/national entities) and Sayari (ownership/risk \
graph -- strongest for cross-border/international entities and sanctions/PEP \
screening).

Routing rule (placeholder -- revisit as real usage patterns emerge):
- National/domestic company or news -> call the ask tool with sources=["cala"].
- International company, cross-border ownership, or sanctions/risk screening \
-> call it with sources=["sayari"].
- Can't tell, or it could reasonably need both -> omit `sources` entirely so \
the tool fans out to both providers.

If the tool response's `conflicts` array is non-empty, Cala and Sayari disagree \
on a fact -- surface both positions to the user, never silently prefer one \
source. Always cite claims back to the tool's citations.
"""


def get_bearer_token(client_id: str, client_secret: str) -> str:
    response = httpx.post(
        COGNITO_TOKEN_URL,
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": SCOPE},
    )
    response.raise_for_status()
    return response.json()["access_token"]


def ask(
    question: str,
    *,
    gateway_url: str,
    client_id: str,
    client_secret: str,
    model_id: str,
    region: str = "eu-west-1",
    silent: bool = False,
) -> str:
    """Run one question through the router agent and return the final answer text.

    `silent=True` suppresses Strands' default stdout streaming (PrintingCallbackHandler)
    -- use that when running as an AgentCore Runtime entrypoint, where nothing reads stdout
    as a terminal; leave it False for interactive CLI use.
    """
    headers = {"authorization": f"Bearer {get_bearer_token(client_id, client_secret)}"}
    mcp_client = MCPClient(lambda: streamablehttp_client(gateway_url, headers=headers, timeout=120))
    model = BedrockModel(model_id=model_id, region_name=region)

    with mcp_client:
        # Bedrock's Converse API restricts tool names to [a-zA-Z0-9_-]+, but a2k-box's own
        # tool names use dots (a2k.ask, a2k.search, ...) and the Gateway namespaces them
        # further as "<target>___a2k.ask" -- both dot-containing and otherwise valid MCP
        # names. Rename for the model only; call_tool_async still uses each tool's original
        # mcp_tool.name to reach the MCP server, so this doesn't touch a2k-box's real interface.
        tools = [
            MCPAgentTool(tool.mcp_tool, tool.mcp_client, name_override=tool.tool_name.replace(".", "_"))
            for tool in mcp_client.list_tools_sync()
        ]
        agent_kwargs = {"model": model, "tools": tools, "system_prompt": SYSTEM_PROMPT}
        if silent:
            agent_kwargs["callback_handler"] = None
        agent = Agent(**agent_kwargs)
        result = agent(question)
        return str(result)
