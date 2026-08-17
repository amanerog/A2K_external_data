"""One-off probe against the AgentCore Gateway fronting a2k-box (the target
configured in README.md step 4), as opposed to test_remote_mcp_iam.py which
hits the Runtime directly. Does the whole round trip in one script: fetches
a Bearer token from the Gateway's inbound Cognito pool (client_credentials
grant -- the pool the console's "Quick create with Cognito" generated,
confirmed live 2026-08-17 to be my-user-pool-278is5ma /
eu-west-1_qpRBYSK8V), then uses it to call initialize + tools/list against
the Gateway's own MCP endpoint.

The Cognito domain and scope below are specific to that pool's
"gateway-mcp-sayari-cala" resource server -- if you recreate the gateway
(and its quick-create pool) from scratch, re-derive these from the Cognito
console (User pools -> that pool -> App integration -> Domain /
Resource servers) rather than assuming they still apply.

Usage:
    export CLIENT_ID="..."       # from the gateway's inbound-auth quick-create
    export CLIENT_SECRET="..."
    export GATEWAY_URL="https://<gateway-id>.gateway.bedrock-agentcore.eu-west-1.amazonaws.com/mcp"
    python test_gateway_mcp.py
"""

import asyncio
import os

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

COGNITO_TOKEN_URL = "https://my-domain-f9bf0du3.auth.eu-west-1.amazoncognito.com/oauth2/token"
SCOPE = "gateway-mcp-sayari-cala/genesis-gateway:invoke"


async def _get_bearer_token() -> str:
    client_id = os.environ["CLIENT_ID"]
    client_secret = os.environ["CLIENT_SECRET"]
    async with httpx.AsyncClient() as http_client:
        response = await http_client.post(
            COGNITO_TOKEN_URL,
            auth=(client_id, client_secret),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "client_credentials", "scope": SCOPE},
        )
        response.raise_for_status()
        return response.json()["access_token"]


async def main() -> None:
    url = os.environ["GATEWAY_URL"]
    token = await _get_bearer_token()
    headers = {"authorization": f"Bearer {token}"}
    async with streamablehttp_client(url, headers, timeout=120) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.list_tools()
            print(f"{len(result.tools)} tools:")
            for tool in result.tools:
                print(f"- {tool.name}: {tool.description}")


if __name__ == "__main__":
    asyncio.run(main())
