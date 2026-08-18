"""Measures the raw byte size of a2k.ask's response through the Gateway, for
a single query, comparing sources=["cala"], sources=["sayari"], and omitted
(fan-out to both) -- to check whether tool result size is what's tripping
Bedrock's "too many total text bytes" context-overflow error (see core.py's
ask(), which goes through the LLM and can't isolate this on its own).

Usage:
    export CLIENT_ID=... CLIENT_SECRET=... GATEWAY_URL=...
    python test_tool_result_size.py "Acme Robotics Inc."
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

from core import get_bearer_token

QUERY = sys.argv[1] if len(sys.argv) > 1 else "Acme Robotics Inc."
TOOL_NAME_SUFFIX = "a2k.ask"


async def _call_ask(session: ClientSession, tool_name: str, sources: list[str] | None) -> None:
    args = {"query": QUERY}
    if sources is not None:
        args["sources"] = sources
    result = await session.call_tool(tool_name, args)
    raw = "".join(block.text for block in result.content if hasattr(block, "text"))
    label = sources if sources is not None else "omitted (fan-out to both)"
    print(f"sources={label}: {len(raw):,} bytes, {len(raw) / 4:,.0f} approx tokens (4 bytes/token rule of thumb)")


async def main() -> None:
    gateway_url = os.environ["GATEWAY_URL"]
    token = get_bearer_token(os.environ["CLIENT_ID"], os.environ["CLIENT_SECRET"])
    headers = {"authorization": f"Bearer {token}"}

    async with streamablehttp_client(gateway_url, headers, timeout=120) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = await session.list_tools()
            ask_tool = next(t.name for t in tools.tools if t.name.endswith(TOOL_NAME_SUFFIX))

            await _call_ask(session, ask_tool, ["cala"])
            await _call_ask(session, ask_tool, ["sayari"])
            await _call_ask(session, ask_tool, None)


if __name__ == "__main__":
    asyncio.run(main())
