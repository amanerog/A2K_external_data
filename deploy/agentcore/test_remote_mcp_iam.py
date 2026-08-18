"""One-off probe against a deployed a2k-box MCP server on AgentCore Runtime,
using IAM/SigV4 auth (boto3's own credential chain) instead of a Cognito/
OAuth Bearer token -- see "Test the deployed server" in this directory's
README.md. Useful when you have AWS_ACCESS_KEY_ID/AWS_SECRET_ACCESS_KEY/
AWS_SESSION_TOKEN (e.g. from SSO) rather than a Cognito user/password.

Not verified in this environment (no AWS credentials in the sandbox this
was written in) -- confirm against your account, and note this only works
if the Runtime's inbound authorizer is IAM, not JWT/Cognito.

`invoke_agent_runtime` has no `mcpMethod`/`mcpName` params (confirmed
against a real botocore ParamValidationError 2026-08-11, despite earlier
docs summaries suggesting otherwise) -- the MCP JSON-RPC `method` lives
entirely inside `payload`. MCP itself requires an `initialize` handshake
before any other call, so this does that first and threads the
`mcpSessionId` AWS returns into the follow-up `tools/list` call.

Usage:
    pip install boto3   # if not already present
    python test_remote_mcp_iam.py
"""

import json
from typing import Optional

import boto3

REGION = "eu-west-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:396961015428:runtime/hosted_agent_68t65-EjboYy7K9o"
MCP_PROTOCOL_VERSION = "2025-06-18"  # one of the versions AgentCore Runtime supports


def _call(client, *, payload: dict, mcp_session_id: Optional[str] = None) -> dict:
    kwargs = dict(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier="DEFAULT",
        payload=json.dumps(payload).encode("utf-8"),
        contentType="application/json",
        # MCP streamable-http requires the client to accept *both* -- the server may
        # respond with an immediate JSON body or switch to an SSE stream; accepting
        # only application/json fails content negotiation with an HTTP 406 (confirmed
        # live 2026-08-18, this script's accept value had never actually been exercised
        # before that -- see module docstring "Not verified").
        accept="application/json, text/event-stream",
        mcpProtocolVersion=MCP_PROTOCOL_VERSION,
    )
    if mcp_session_id:
        kwargs["mcpSessionId"] = mcp_session_id

    response = client.invoke_agent_runtime(**kwargs)
    body = response["response"].read().decode("utf-8")
    print(f"--- {payload['method']} -> statusCode={response['statusCode']} mcpSessionId={response.get('mcpSessionId')} ---")
    print(body)
    print()
    return response


def main() -> None:
    client = boto3.client("bedrock-agentcore", region_name=REGION)

    init_response = _call(
        client,
        payload={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "test_remote_mcp_iam", "version": "1.0"},
            },
        },
    )

    mcp_session_id = init_response.get("mcpSessionId")

    _call(
        client,
        payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        mcp_session_id=mcp_session_id,
    )


if __name__ == "__main__":
    main()
