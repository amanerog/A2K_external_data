"""AgentCore Runtime entrypoint for the a2k-box MCP server.

Copy this file (and requirements.txt in this same directory) into the
project folder that `agentcore create --protocol MCP` scaffolds, alongside
a full copy of the `a2k/` package from the repo root. Point agentcore.json's
`entrypoint` field at this file. See README "Deploy to AgentCore".
"""

from a2k.mcp_server.server import main

if __name__ == "__main__":
    main()
