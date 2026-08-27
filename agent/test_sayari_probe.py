"""Ad-hoc probe against Sayari, showing what actually gets called and what
comes back -- two stages, in order:

1. **Direct against Sayari's own hosted MCP server** (bypasses the Gateway
   and the deployed Runtime entirely) -- runs a2k-box's `SayariMcpAdapter`
   (adapters/sayari_mcp.py) locally, live, with its private `_call_tool`
   wrapped so every tool call it makes -- name and exact arguments --
   prints directly, plainly, as it happens (not via gateway/tracing.py's
   opt-in JSON trace lines -- this doesn't depend on A2K_TRACE_CALLS at all,
   so it's always visible regardless of that flag). Needs Sayari's *MCP*
   Auth0 credentials (`AUTH0_CLIENT_ID`/`AUTH0_CLIENT_SECRET`) -- a
   **different** credential pair from `CLIENT_ID`/`CLIENT_SECRET` below
   (that's the Gateway's inbound auth) -- see adapters/sayari_mcp.py's
   module docstring for why they're not interchangeable.
2. **Through the deployed Gateway** -- as before, showing the Facts/claims
   a2k-box produced *from* Sayari's response.

Two modes, picked by what you pass on the command line:

- **Free-text query** (default) -- `search_entities` first (name/text
  match), then `get_entity_summary` per candidate. Gateway side calls
  `a2k.search`/`a2k.ask`. This is a *name search* -- passing a full
  sentence or an entity_id embedded in prose won't match anything in
  Sayari's search index and comes back with 0 results (confirmed: that's
  exactly what happened querying "Dame la informacion... entity id es
  9Ecv5KcN6_BsR7gicQMeYw" as free text -- search_entities doesn't parse an
  ID out of a sentence, it just doesn't match).
- **`--entity-id <id>`** -- when you already know the Sayari entity_id, skip
  `search_entities` entirely and call `get_entity_summary` directly with
  it (same as `adapter.get_document()`/a2k-box's own `get_document()`
  contract). Gateway side calls `a2k.getDocument` instead of
  `a2k.search`/`a2k.ask`, since those two only take a free-text `query`,
  not an entity_id -- there's no "ask by ID" tool other than getDocument.
- **`--schema`** -- doesn't call either tool. Opens a session straight to
  Sayari's own MCP server and dumps `list_tools()`'s real `inputSchema` for
  every tool it exposes (not just the two a2k-box's adapter uses). Answers
  "does search_entities support filtering by risk/sanctions natively"
  authoritatively instead of guessing from the module docstring's summary
  of the shape -- a2k.search/a2k.ask have no LLM interpreting the query at
  the a2k-box level (gateway/synthesis.py is templated assembly, not
  reasoning), so if there's no such parameter here, no amount of rephrasing
  the query text will filter by sanctions on its own.

Local `A2K_BOX_MODE` only affects step 1 (run in this process) -- step 2
always reflects whatever mode the *deployed* Runtime is actually configured
with, regardless of this env var. In mock mode, step 1 has nothing to show
(mock search/get_document never calls Sayari at all), so it's skipped with
a warning.

Usage:
    export CLIENT_ID=... CLIENT_SECRET=... GATEWAY_URL=...      # Gateway (step 2)
    export AUTH0_CLIENT_ID=... AUTH0_CLIENT_SECRET=...          # Sayari MCP direct (step 1)
    export A2K_BOX_MODE=live                                    # step 1 only

    python test_sayari_probe.py "Ali Ansari"
    python test_sayari_probe.py --entity-id 9Ecv5KcN6_BsR7gicQMeYw
    python test_sayari_probe.py --schema
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import httpx

# agent/ has no __init__.py (see core.py's flat "from core import ..." convention) and
# a2k/ lives one directory up -- add the repo root to sys.path so `import a2k...` resolves
# regardless of whether this is run from the repo root or from inside agent/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcp import ClientSession  # noqa: E402
from mcp.client.streamable_http import streamablehttp_client  # noqa: E402

from core import get_bearer_token  # noqa: E402

from a2k.adapters.sayari_mcp import SayariMcpAdapter  # noqa: E402
from a2k.config import config  # noqa: E402

SCHEMA_MODE = len(sys.argv) > 1 and sys.argv[1] == "--schema"

if not SCHEMA_MODE and len(sys.argv) > 2 and sys.argv[1] == "--entity-id":
    QUERY = None
    ENTITY_ID = sys.argv[2]
else:
    QUERY = sys.argv[1] if (not SCHEMA_MODE and len(sys.argv) > 1) else "Acme Robotics Inc."
    ENTITY_ID = None

SOURCES = ["sayari"]
KB_ID = "urn:a2k:vendor:sayari"


def _print_result(label: str, result) -> None:
    raw = "".join(block.text for block in result.content if hasattr(block, "text"))
    print(f"\n=== {label} ===")
    try:
        parsed = json.loads(raw)
        print(json.dumps(parsed, indent=2, ensure_ascii=False))
    except json.JSONDecodeError:
        print(raw)  # not JSON (e.g. an error string) -- print as-is rather than crash


def _wrap_call_tool(adapter: SayariMcpAdapter) -> None:
    """Wraps `_call_tool` (adapters/sayari_mcp.py) directly rather than
    relying on gateway/tracing.py's opt-in trace lines -- that mechanism is
    meant for CloudWatch, gated behind A2K_TRACE_CALLS and printed as
    one-JSON-per-line; this wrapper is just for this script, always on,
    plainly formatted."""
    original_call_tool = adapter._call_tool

    async def _print_then_call(session, tool: str, arguments: dict):
        print(f"  >> tool called: {tool}")
        print(f"     arguments:   {json.dumps(arguments, ensure_ascii=False)}")
        result = await original_call_tool(session, tool, arguments)
        print("     <- returned OK")
        return result

    adapter._call_tool = _print_then_call


async def _probe_sayari_mcp_directly() -> None:
    """Runs a2k-box's own Sayari adapter locally, live, printing every tool
    call it makes against Sayari's hosted MCP server as it happens. Bypasses
    the Gateway/deployed Runtime entirely; it's the same adapter code as
    production, just run in this process instead of AgentCore."""
    adapter = SayariMcpAdapter()
    _wrap_call_tool(adapter)

    if ENTITY_ID:
        print(f"\n=== Sayari MCP tool calls (direct, live) for entity_id={ENTITY_ID!r} ===")
        doc = await adapter.get_document(f"{KB_ID}:doc:{ENTITY_ID}:profile")
        print(f"--- {'found' if doc else 'not found'} ---")
    else:
        print(f"\n=== Sayari MCP tool calls (direct, live) for {QUERY!r} ===")
        facts = await adapter.search(QUERY, limit=10)
        print(f"--- {len(facts)} facts extracted from the tool calls above ---")


async def _print_sayari_tool_schemas() -> None:
    """Connects straight to Sayari's own hosted MCP server (not through
    a2k-box/the Gateway) and dumps every tool's real `inputSchema` -- the
    authoritative answer to "what parameters does search_entities actually
    accept", instead of inferring it from what a2k-box's adapter happens to
    use (adapters/sayari_mcp.py only ever passes query/limit; that's a
    choice, not proof there's nothing else available).

    Reuses SayariMcpAdapter's private `_get_token()`/`_open_session()`
    rather than duplicating the Auth0 handshake -- same reasoning as
    `_wrap_call_tool` reaching into `_call_tool` above: this is a one-off
    diagnostic script, not production code, so borrowing internals directly
    is fine here.
    """
    if config.is_mock:
        print(
            "A2K_BOX_MODE is not 'live' locally -- --schema needs a real Auth0 token, "
            "so there's nothing to fetch in mock mode. Set A2K_BOX_MODE=live plus "
            "AUTH0_CLIENT_ID/AUTH0_CLIENT_SECRET.",
            file=sys.stderr,
        )
        return

    adapter = SayariMcpAdapter()
    token = await adapter._get_token()
    headers = {"Authorization": f"Bearer {token}"}

    async with httpx.AsyncClient(headers=headers, timeout=60.0, verify=config.httpx_verify) as http_client:
        async with adapter._open_session(http_client) as streams:
            read, write = streams[0], streams[1]
            async with ClientSession(read, write) as session:
                await session.initialize()
                tools = await session.list_tools()
                print(f"\n=== Sayari MCP -- {len(tools.tools)} tools, full inputSchema ===")
                for tool in tools.tools:
                    print(f"\n--- {tool.name} ---")
                    print(tool.description or "(no description)")
                    print(json.dumps(tool.inputSchema, indent=2, ensure_ascii=False))


async def main() -> None:
    if SCHEMA_MODE:
        await _print_sayari_tool_schemas()
        return

    if config.is_mock:
        print(
            "A2K_BOX_MODE is not 'live' locally -- skipping the direct Sayari MCP probe "
            "(mock mode never calls Sayari, so there'd be nothing real to show). Set "
            "A2K_BOX_MODE=live (plus AUTH0_CLIENT_ID/AUTH0_CLIENT_SECRET) to see it. The "
            "Gateway calls below are unaffected either way -- they reflect the *deployed* "
            "Runtime's own mode, not this local env var.",
            file=sys.stderr,
        )
    else:
        await _probe_sayari_mcp_directly()

    if not (os.environ.get("GATEWAY_URL") and os.environ.get("CLIENT_ID") and os.environ.get("CLIENT_SECRET")):
        print(
            "\nGATEWAY_URL/CLIENT_ID/CLIENT_SECRET not all set -- skipping the Gateway stage "
            "(a2k.search/a2k.ask/a2k.getDocument). That's the Gateway's own inbound auth, "
            "unrelated to Sayari's AUTH0_CLIENT_ID/AUTH0_CLIENT_SECRET above -- the direct "
            "Sayari MCP probe works fine without it.",
            file=sys.stderr,
        )
        return

    gateway_url = os.environ["GATEWAY_URL"]
    token = get_bearer_token(os.environ["CLIENT_ID"], os.environ["CLIENT_SECRET"])
    headers = {"authorization": f"Bearer {token}"}

    async with streamablehttp_client(gateway_url, headers, timeout=120) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()

            if ENTITY_ID:
                get_document_tool = next(t.name for t in tools.tools if t.name.endswith("a2k.getDocument"))
                document_id = f"{KB_ID}:doc:{ENTITY_ID}:profile"
                result = await session.call_tool(get_document_tool, {"documentId": document_id})
                _print_result(f"a2k.getDocument (documentId={document_id!r}, via Gateway)", result)
            else:
                search_tool = next(t.name for t in tools.tools if t.name.endswith("a2k.search"))
                ask_tool = next(t.name for t in tools.tools if t.name.endswith("a2k.ask"))

                search_result = await session.call_tool(search_tool, {"query": QUERY, "sources": SOURCES})
                _print_result("a2k.search (raw Sayari facts/passages, via Gateway)", search_result)

                ask_result = await session.call_tool(ask_tool, {"query": QUERY, "sources": SOURCES})
                _print_result("a2k.ask (Sayari facts, synthesized into claims, via Gateway)", ask_result)


if __name__ == "__main__":
    asyncio.run(main())
