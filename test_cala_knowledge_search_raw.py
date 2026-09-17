"""Ad-hoc probe against Cala's own hosted MCP server (bypasses a2k-box, the
Gateway, and the agent entirely) -- calls `knowledge_search` directly and
prints Cala's *own* LLM-synthesized answer (`content`), which
adapters/cala_mcp.py deliberately discards in the normal a2k-box pipeline
(see that module's docstring: only `explainability[]`'s atomic, cited
claims are used there, never the flowing prose, to keep a2k.ask's own
synthesis independently verifiable).

This is purely for comparing "Cala's own answer" vs. "a2k-box's re-synthesized,
cited answer" -- not something the deployed pipeline uses.

Usage:
    export CALA_API_KEY=...
    python test_cala_knowledge_search_raw.py "your question about a company"
"""

import asyncio
import json
import os
import sys

from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

CALA_MCP_URL = os.environ.get("CALA_MCP_URL", "https://api.cala.ai/mcp/")
QUERY = sys.argv[1] if len(sys.argv) > 1 else "What do we know about Acme Robotics Inc.?"


async def main() -> None:
    api_key = os.environ["CALA_API_KEY"]
    async with streamablehttp_client(CALA_MCP_URL, {"X-API-KEY": api_key}) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("knowledge_search", {"input": QUERY})
            response = result.structuredContent

            print("=== Cala's own synthesized answer (content) ===\n")
            print(response.get("content", "<no content field>"))

            print("\n=== explainability[] (what a2k-box actually uses) ===\n")
            print(json.dumps(response.get("explainability", []), indent=2))


if __name__ == "__main__":
    asyncio.run(main())
