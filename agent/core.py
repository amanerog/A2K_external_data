"""Cala/Sayari router agent -- shared by router_agent.py (local CLI) and
entrypoint.py (AgentCore Runtime deploy) so both stay in sync.

Answers company-intelligence questions by calling a2k-box's tools through
the AgentCore Gateway (../deploy/agentcore/README.md section 4), routing
between Cala and Sayari via the `sources` param a2k.ask already exposes
(a2k/mcp_server/server.py) -- this doesn't add a new mechanism, it just
steers the existing tool.

Routing: no hardcoded national/international-style rule -- `sources` is
picked based on each vendor's actual declared coverage (domains/topics/
coverage.scope/status/priority, from the KB Cards in a2k/cards/*.json, via
a2k.listVendors in a2k/mcp_server/server.py). Confirmed live 2026-08-18 that
leaving this as "the model calls listVendors when it decides to" doesn't
work reliably -- across three test questions (agent/test_routing_behavior.py)
the model never called listVendors even once, always either omitted
`sources` or split one question into multiple ask calls instead. So this is
now deterministic: `ask()` fetches and formats the catalogue itself before
building the Agent, injects it directly into the system prompt, and drops
listVendors from the tools handed to the model entirely -- there's nothing
left for the model to skip. Routing judgment (which sourceId(s) match the
question) is still the model's job; only the catalogue *lookup* was made
non-optional.

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
import threading
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

SYSTEM_PROMPT_TEMPLATE = """You are a company-intelligence assistant, sitting \
between a caller and one or more vendor knowledge sources behind a single \
gateway. You reason about which vendor(s) a question needs; you do not need \
to answer from your own knowledge -- the ask tool returns an already-cited, \
already-synthesized answer built only from what the vendor(s) actually \
returned, and your job is to relay that faithfully, not to add facts of \
your own.

VENDOR SELECTION. The current vendor catalogue (already fetched for you --
do not look for a tool to re-fetch it, there isn't one; this list is
current as of this request):

{vendor_catalogue}

1. Only vendors listed above with status "active" are eligible -- never \
pass an inactive `sourceId` to the ask tool, no matter how well its \
domains/topics match.
2. Match the question against the active vendors' domains/topics/scope \
above -- not against assumptions about the vendors' names.
3. Exactly one active vendor is a clear match -> pass that one `sourceId` \
as the ask tool's `sources` param. `priority` (lower = preferred) is \
available if you ever need a tie-breaker between two plausible matches, \
though with only two vendors today their topics rarely overlap enough to \
need it.
4. More than one active vendor plausibly matches, or none clearly does -> \
omit `sources` entirely so the tool fans out to all active vendors. Do not \
guess a single vendor just to avoid a fan-out call -- an unnecessary second \
vendor in the answer is a smaller problem than silently dropping a vendor \
that had the answer.
5. If the catalogue above is empty or every vendor is inactive, do not call \
the ask tool at all -- tell the caller no vendor is currently available \
rather than guessing.

Call the ask tool at most once per question. Its `sources` param already \
fans out to multiple vendors when omitted -- do not call it once per \
vendor, and do not follow up with the search tool just to double-check an \
ask result that already answered the question. Only make a second tool \
call if the first response is genuinely insufficient (e.g. explicitly says \
no data found and a differently-scoped query might help). If the ask tool \
itself fails, times out, or errors, say so plainly -- never fabricate an \
answer to cover for a failed tool call.

If the tool response's `conflicts` array is non-empty, the vendors disagree \
on a fact -- surface both positions to the user, never silently prefer one \
source. Always cite claims back to the tool's citations.

Never surface, ask for, or repeat any vendor credential or token -- you \
never see them; the tools handle that entirely on their own.

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
# Guards both caches' dict reads/writes below (not the token HTTP call or the
# MCPClient build itself -- those stay outside the lock on purpose). Holding
# this lock across MCPClient.start() would deadlock: MCPClient calls
# get_bearer_token() back from its own background thread (via _transport()
# below), which also wants this same lock. So this narrows, but doesn't
# fully close, the window where two truly concurrent first-calls could both
# miss the cache and both build a client/fetch a token -- the second write
# just overwrites the first in the dict (a wasted duplicate build/fetch, not
# a data race), rather than corrupting the dict itself. AgentCore Runtime's
# concurrency model within one warm container isn't documented as strictly
# single-request-at-a-time, hence locking the dict ops at all.
_cache_lock = threading.Lock()


def get_bearer_token(client_id: str, client_secret: str) -> str:
    with _cache_lock:
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
    with _cache_lock:
        _token_cache[client_id] = (token, time.time() + expires_in - _TOKEN_EXPIRY_MARGIN_SECONDS)
    return token


_LIST_VENDORS_MCP_NAME = "a2k.listVendors"
_NO_VENDORS_CATALOGUE_TEXT = "(catalogue came back empty -- treat as no vendors available, see rule 5.)"


def _format_vendor_catalogue(catalogue: dict) -> str:
    vendors = catalogue.get("vendors") or []
    if not vendors:
        return _NO_VENDORS_CATALOGUE_TEXT
    blocks = []
    for v in vendors:
        blocks.append(
            f"- sourceId={v.get('sourceId')!r}  name={v.get('name')!r}  "
            f"status={v.get('status')!r}  priority={v.get('priority')!r}\n"
            f"  domains={v.get('domains')!r}\n"
            f"  topics={v.get('topics')!r}\n"
            f"  scope: {v.get('scope')}"
        )
    return "\n".join(blocks)


def _fetch_vendor_catalogue_text(mcp_client: MCPClient, tools: list[MCPAgentTool]) -> str:
    """Calls a2k.listVendors ourselves (not left to the model -- see module
    docstring "Routing") and formats the result for direct injection into the
    system prompt."""
    # Matching by suffix, not equality: through the Gateway, tool names carry a
    # "<target-name>___" prefix (e.g. "target-quick-start-h9x11y___a2k.listVendors"),
    # confirmed live 2026-08-18 -- an exact match against the bare a2k-box name never
    # matched, so this fetch (and, separately, the tools-list exclusion below) silently
    # fell back to "not found" every time even once the Gateway target was in sync.
    list_vendors = next((t for t in tools if t.mcp_tool.name.endswith(_LIST_VENDORS_MCP_NAME)), None)
    if list_vendors is None:
        return "(listVendors tool not found on this Gateway -- fan out to all sources; do not restrict `sources`.)"

    result = mcp_client.call_tool_sync(
        tool_use_id="prefetch-vendor-catalogue", name=list_vendors.mcp_tool.name, arguments={}
    )
    if result.get("status") != "success" or not result.get("structuredContent"):
        return "(fetching the vendor catalogue failed -- fan out to all sources; do not restrict `sources`.)"
    return _format_vendor_catalogue(result["structuredContent"])


# Cached (MCPClient, tools handed to the LLM, formatted vendor catalogue text)
# keyed by (gateway_url, client_id) -- see module docstring "Latency". MCPClient
# itself is documented as reusable across calls ("allowing reuse of the same
# connection for multiple tool calls to reduce latency"); we're just holding
# onto that reuse across `ask()` calls instead of opening/closing a fresh one
# every time -- the vendor catalogue is fetched once per cache entry for the
# same reason, not re-fetched on every question (coverage doesn't change that
# often; bump the cache key or restart the process if it ever does mid-session).
# Keyed on client_id too, not just gateway_url, so two different credential
# sets against the same Gateway (not a real scenario in either deployed
# Runtime today, both use one fixed credential set, but not guaranteed to
# stay that way) can't collide and silently reuse each other's connection.
_mcp_cache: dict[tuple[str, str], tuple[MCPClient, list[MCPAgentTool], str]] = {}


def _get_tools_and_catalogue(gateway_url: str, client_id: str, client_secret: str) -> tuple[list[MCPAgentTool], str]:
    cache_key = (gateway_url, client_id)
    with _cache_lock:
        cached = _mcp_cache.get(cache_key)
        if cached is not None:
            return cached[1], cached[2]

    def _transport():
        # Re-fetches (or returns the cached, still-valid) token on every reconnect
        # attempt, not just the first -- transport_callable can be invoked again by
        # MCPClient on its own reconnect logic, potentially after the first token expired.
        headers = {"authorization": f"Bearer {get_bearer_token(client_id, client_secret)}"}
        return streamablehttp_client(gateway_url, headers=headers, timeout=120)

    mcp_client = MCPClient(_transport)
    mcp_client.start()
    all_tools = mcp_client.list_tools_sync()
    catalogue_text = _fetch_vendor_catalogue_text(mcp_client, all_tools)

    # Bedrock's Converse API restricts tool names to [a-zA-Z0-9_-]+, but a2k-box's own
    # tool names use dots (a2k.ask, a2k.search, ...) and the Gateway namespaces them
    # further as "<target>___a2k.ask" -- both dot-containing and otherwise valid MCP
    # names. Rename for the model only; call_tool_async still uses each tool's original
    # mcp_tool.name to reach the MCP server, so this doesn't touch a2k-box's real interface.
    # listVendors itself is dropped here -- its result is injected into the system
    # prompt above instead, so there's nothing left for the model to (redundantly, or
    # unreliably -- see module docstring "Routing") call it for.
    tools = [
        MCPAgentTool(tool.mcp_tool, tool.mcp_client, name_override=tool.tool_name.replace(".", "_"))
        for tool in all_tools
        if not tool.mcp_tool.name.endswith(_LIST_VENDORS_MCP_NAME)
    ]
    with _cache_lock:
        _mcp_cache[cache_key] = (mcp_client, tools, catalogue_text)
    return tools, catalogue_text


def _evict_mcp_cache(gateway_url: str, client_id: str) -> None:
    with _cache_lock:
        cached = _mcp_cache.pop((gateway_url, client_id), None)
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
    tools, catalogue_text = _get_tools_and_catalogue(gateway_url, client_id, client_secret)
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(vendor_catalogue=catalogue_text)

    agent_kwargs = {"model": model, "tools": tools, "system_prompt": system_prompt}
    if silent:
        agent_kwargs["callback_handler"] = None
    agent = Agent(**agent_kwargs)

    try:
        result = agent(question)
    except Exception:
        # Cached connection may have gone stale (Gateway-side session timeout, network
        # blip) -- evict so the *next* call rebuilds clean, then re-raise this one as a
        # failure rather than trying to recover a possibly-half-broken session mid-request.
        _evict_mcp_cache(gateway_url, client_id)
        raise
    return str(result)
