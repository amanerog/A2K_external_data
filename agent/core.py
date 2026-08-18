"""Cala/Sayari router agent -- shared by router_agent.py (local CLI) and
entrypoint.py (AgentCore Runtime deploy) so both stay in sync.

Answers company-intelligence questions by calling a2k-box's tools through
the AgentCore Gateway (../deploy/agentcore/README.md section 4), routing
between Cala and Sayari via the `sources` param a2k.ask already exposes
(a2k/mcp_server/server.py) -- this doesn't add a new mechanism, it just
steers the existing tool.

Routing: no hardcoded national/international-style rule anymore -- the model
calls a2k.listVendors (a2k/mcp_server/server.py) to read each vendor's actual
declared coverage (domains/topics/coverage.scope, from the KB Cards in
a2k/cards/*.json) and picks `sources` based on that, per the system prompt
below. Keeps this agent dumb/stateless (no classification code here) and
keeps routing grounded in what the vendors actually declare instead of an
assumption baked into a prompt.

Auth: Cognito client_credentials flow (this Gateway's inbound identity --
domain/scope confirmed live 2026-08-17 against the my-user-pool-278is5ma
pool's "gateway-mcp-sayari-cala" resource server, see
../deploy/agentcore/test_gateway_mcp.py's docstring for how those were
found. Re-derive from the Cognito console if the gateway is ever recreated).

Latency: AgentCore Runtime keeps a container warm across invocations within
a session (it's not a fresh cold process per call the way Lambda often is),
so module-level caching below actually pays off across calls, not just
within one. Two things were previously re-done on every single `ask()`
call and are now cached at module scope instead: the Cognito bearer token
(was a fresh OAuth round-trip every time; Cognito's own `expires_in` now
governs re-fetch) and the Gateway MCP connection + tool list (was a fresh
`initialize` + `tools/list` handshake every time; AWS's own MCP-server-
targets doc calls this out explicitly as avoidable latency for
Runtime-hosted targets like a2k-box). If the cached connection goes stale
(Gateway-side session timeout, network blip), `ask()` evicts both caches
and lets the *next* call rebuild clean rather than trying to recover
mid-request.
"""

from __future__ import annotations

import json
import os
import time
from functools import lru_cache

import httpx
from mcp.client.streamable_http import streamablehttp_client
from strands import Agent
from strands.models import BedrockModel
from strands.tools.mcp import MCPClient
from strands.tools.mcp.mcp_agent_tool import MCPAgentTool

COGNITO_TOKEN_URL = "https://my-domain-f9bf0du3.auth.eu-west-1.amazoncognito.com/oauth2/token"
SCOPE = "gateway-mcp-sayari-cala/genesis-gateway:invoke"

SYSTEM_PROMPT = """You are a company-intelligence assistant. Your tools query \
one or more vendor knowledge sources behind a single gateway.

Before your first ask/search call in a conversation, call listVendors once to \
see what each vendor actually covers (`domains`, `topics`, and a human-readable \
`scope` for each `sourceId`). Match the question against that -- not against \
assumptions about the vendors' names -- and pass the matching `sourceId`(s) as \
the ask tool's `sources` param. If more than one vendor's declared coverage \
plausibly matches, or none clearly does, omit `sources` entirely so the tool \
fans out to all vendors rather than guessing wrong. You don't need to call \
listVendors again later in the same conversation -- vendor coverage doesn't \
change mid-conversation.

Call the ask tool at most once per question. Its `sources` param already \
fans out to both providers when omitted -- do not call it once per source, \
and do not follow up with the search tool just to double-check an ask \
result that already answered the question. Only make a second tool call if \
the first response is genuinely insufficient (e.g. explicitly says no data \
found and a differently-scoped query might help).

If the tool response's `conflicts` array is non-empty, Cala and Sayari disagree \
on a fact -- surface both positions to the user, never silently prefer one \
source. Always cite claims back to the tool's citations.

If the ask tool's response has a `content` field instead of the normal \
`ok`/`claims`/`citations` envelope, that means Cala's own answer is being \
returned unprocessed (a test mode, not the normal path). When you see this \
shape, reply with that `content` text verbatim -- word for word, same \
language, no paraphrasing, no summarizing, no reformatting, no added \
commentary before or after it. Do not treat it as a normal tool result to \
synthesize an answer from.
"""


@lru_cache(maxsize=1)
def _secrets_manager_bundle() -> dict[str, str]:
    """Live-credential fallback so entrypoint.py can point AGENT_SECRETS_ARN at a
    Secrets Manager secret (flat JSON, same env var names) instead of putting
    CLIENT_ID/CLIENT_SECRET in plaintext AgentCore Runtime environment variables,
    which are visible to anyone with read access to the Runtime resource. Only
    consulted when a name is genuinely unset -- router_agent.py's plain shell env
    vars for local testing are unaffected."""
    secret_arn = os.environ.get("AGENT_SECRETS_ARN")
    if not secret_arn:
        return {}
    import boto3

    client = boto3.client("secretsmanager")
    return json.loads(client.get_secret_value(SecretId=secret_arn)["SecretString"])


def secret_env(name: str) -> str | None:
    return os.environ.get(name) or _secrets_manager_bundle().get(name)


# (token, expiry epoch seconds) keyed by client_id -- module-level, so it
# survives across `ask()` calls within the same warm Runtime container.
_token_cache: dict[str, tuple[str, float]] = {}
# 60s safety margin so a token doesn't expire mid-request.
_TOKEN_EXPIRY_MARGIN_SECONDS = 60


def get_bearer_token(client_id: str, client_secret: str) -> str:
    cached = _token_cache.get(client_id)
    if cached and cached[1] > time.time():
        return cached[0]

    response = httpx.post(
        COGNITO_TOKEN_URL,
        auth=(client_id, client_secret),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        data={"grant_type": "client_credentials", "scope": SCOPE},
    )
    response.raise_for_status()
    body = response.json()
    token = body["access_token"]
    expires_in = body.get("expires_in", 3600)
    _token_cache[client_id] = (token, time.time() + expires_in - _TOKEN_EXPIRY_MARGIN_SECONDS)
    return token


# Cached (MCPClient, sanitized tools) keyed by gateway_url -- see module
# docstring "Latency". MCPClient itself is documented as reusable across
# calls ("allowing reuse of the same connection for multiple tool calls to
# reduce latency"); we're just holding onto that reuse across `ask()` calls
# instead of opening/closing a fresh one every time.
_mcp_cache: dict[str, tuple[MCPClient, list[MCPAgentTool]]] = {}


def _get_tools(gateway_url: str, client_id: str, client_secret: str) -> list[MCPAgentTool]:
    cached = _mcp_cache.get(gateway_url)
    if cached is not None:
        return cached[1]

    def _transport():
        # Re-fetches (or returns the cached, still-valid) token on every reconnect
        # attempt, not just the first -- transport_callable can be invoked again by
        # MCPClient on its own reconnect logic, potentially after the first token expired.
        headers = {"authorization": f"Bearer {get_bearer_token(client_id, client_secret)}"}
        return streamablehttp_client(gateway_url, headers=headers, timeout=120)

    mcp_client = MCPClient(_transport)
    mcp_client.start()
    # Bedrock's Converse API restricts tool names to [a-zA-Z0-9_-]+, but a2k-box's own
    # tool names use dots (a2k.ask, a2k.search, ...) and the Gateway namespaces them
    # further as "<target>___a2k.ask" -- both dot-containing and otherwise valid MCP
    # names. Rename for the model only; call_tool_async still uses each tool's original
    # mcp_tool.name to reach the MCP server, so this doesn't touch a2k-box's real interface.
    tools = [
        MCPAgentTool(tool.mcp_tool, tool.mcp_client, name_override=tool.tool_name.replace(".", "_"))
        for tool in mcp_client.list_tools_sync()
    ]
    _mcp_cache[gateway_url] = (mcp_client, tools)
    return tools


def _evict_mcp_cache(gateway_url: str) -> None:
    cached = _mcp_cache.pop(gateway_url, None)
    if cached is not None:
        try:
            cached[0].stop(None, None, None)
        except Exception:
            pass  # best-effort -- the connection is already presumed broken


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
    model = BedrockModel(model_id=model_id, region_name=region)
    tools = _get_tools(gateway_url, client_id, client_secret)

    agent_kwargs = {"model": model, "tools": tools, "system_prompt": SYSTEM_PROMPT}
    if silent:
        agent_kwargs["callback_handler"] = None
    agent = Agent(**agent_kwargs)

    try:
        result = agent(question)
    except Exception:
        # Cached connection may have gone stale (Gateway-side session timeout, network
        # blip) -- evict so the *next* call rebuilds clean, then re-raise this one as a
        # failure rather than trying to recover a possibly-half-broken session mid-request.
        _evict_mcp_cache(gateway_url)
        raise
    return str(result)
