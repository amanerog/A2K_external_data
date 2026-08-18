"""Verifies which shape a2k.ask is actually returning for Cala right now --
Cala's raw prose (`content`, config.cala_raw_knowledge_search=true on the
a2k-box Runtime) vs. the normal cited envelope (`ok`/`claims`/`citations`,
the default). Calls through the Gateway via MCP directly, same auth as
test_gateway_mcp.py -- deliberately does NOT go through core.py's LLM agent,
since the agent would just paraphrase either shape into its own prose and
hide which one it actually received.

Usage:
    export CLIENT_ID=... CLIENT_SECRET=... GATEWAY_URL=...
    python test_cala_raw_mode.py "your question about a company"
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from core import get_bearer_token

QUERY = sys.argv[1] if len(sys.argv) > 1 else "¿Qué sabemos de Acme Robotics Inc.?"


async def main() -> None:
    gateway_url = os.environ["GATEWAY_URL"]
    token = get_bearer_token(os.environ["CLIENT_ID"], os.environ["CLIENT_SECRET"])
    headers = {"authorization": f"Bearer {token}"}

    async with streamablehttp_client(gateway_url, headers, timeout=120) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            ask_tool = next(t.name for t in tools.tools if t.name.endswith("a2k.ask"))

            result = await session.call_tool(ask_tool, {"query": QUERY, "sources": ["cala"]})
            raw = "".join(block.text for block in result.content if hasattr(block, "text"))
            data = json.loads(raw)

    print(json.dumps(data, indent=2)[:2000])
    print("...\n" if len(json.dumps(data)) > 2000 else "\n")

    if "content" in data and "ok" not in data:
        print("RAW MODE: response is Cala's own prose (`content`) -- CALA_RAW_KNOWLEDGE_SEARCH is active.")
    elif "ok" in data:
        print("NORMAL MODE: response is the cited envelope (`ok`/`claims`/`citations`) -- flag is off (or unset).")
    else:
        print(f"UNRECOGNIZED shape -- top-level keys: {list(data.keys())}")


if __name__ == "__main__":
    asyncio.run(main())
