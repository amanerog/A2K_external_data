"""One-off probe against the deployed router agent on AgentCore Runtime,
using IAM/SigV4 auth (boto3's own credential chain) -- this Runtime's
inbound authorizer is IAM (the default, no custom JWT authorizer was
configured when it was created), unlike a2k-box's own Runtime which uses
Cognito/JWT. See ../deploy/agentcore/test_remote_mcp_iam.py for the
equivalent against a2k-box directly (MCP protocol, needs an initialize
handshake) -- this one is simpler: HTTP protocol, one request in, one
response out, no session/handshake involved.

Usage:
    python test_router_agent_iam.py "¿Qué sabemos de Acme Robotics Inc.?"
"""

import json
import sys

import boto3

REGION = "eu-west-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:396961015428:runtime/a2k_agent-06B5R9CAuJ"


def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "¿Qué sabemos de Acme Robotics Inc.?"

    client = boto3.client("bedrock-agentcore", region_name=REGION)
    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier="DEFAULT",
        payload=json.dumps({"prompt": question}).encode("utf-8"),
        contentType="application/json",
        accept="application/json",
    )
    body = response["response"].read().decode("utf-8")
    print(f"statusCode={response['statusCode']}")
    print(body)


if __name__ == "__main__":
    main()
