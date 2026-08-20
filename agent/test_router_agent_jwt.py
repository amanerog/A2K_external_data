"""One-off probe against the deployed router agent on AgentCore Runtime,
using JWT/Cognito Bearer token auth. This Runtime's inbound authorizer was
switched from the IAM default to a custom JWT authorizer (Cognito) on
2026-08-19, specifically so it can be invoked with a plain Bearer token
(e.g. via curl) instead of a SigV4-signed call -- boto3's
invoke_agent_runtime cannot do bearer-token auth at all; per AWS's own
"Authenticate and authorize with Inbound Auth and Outbound Auth" doc, a
JWT-authorized Runtime must be called via a raw HTTPS POST instead. Replaces
the old test_router_agent_iam.py, which no longer works -- an AgentCore
Runtime supports either IAM SigV4 or JWT Bearer Token inbound auth, never
both at once.

Needs its own Cognito pool/client (separate from the Gateway's
CLIENT_ID/CLIENT_SECRET client_credentials pair used for the agent's own
outbound call to the Gateway, and separate from a2k-box's inbound identity)
-- a human-login (ALLOW_USER_PASSWORD_AUTH) pool created specifically so a
human/script can fetch a Bearer token for calling this Runtime. See
README.md for the setup commands.

Usage:
    export AGENT_CLIENT_ID=... AGENT_USERNAME=... AGENT_PASSWORD=...
    python test_router_agent_jwt.py "¿Qué sabemos de Acme Robotics Inc.?"
"""

import os
import sys
import urllib.parse

import boto3
import httpx

REGION = "eu-west-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:396961015428:runtime/a2k_agent-06B5R9CAuJ"


def _get_bearer_token() -> str:
    client = boto3.client("cognito-idp", region_name=REGION)
    response = client.initiate_auth(
        ClientId=os.environ["AGENT_CLIENT_ID"],
        AuthFlow="USER_PASSWORD_AUTH",
        AuthParameters={
            "USERNAME": os.environ["AGENT_USERNAME"],
            "PASSWORD": os.environ["AGENT_PASSWORD"],
        },
    )
    return response["AuthenticationResult"]["AccessToken"]


def main() -> None:
    question = sys.argv[1] if len(sys.argv) > 1 else "¿Qué sabemos de Acme Robotics Inc.?"

    token = _get_bearer_token()
    encoded_arn = urllib.parse.quote(AGENT_RUNTIME_ARN, safe="")
    url = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"

    response = httpx.post(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        },
        json={"prompt": question},
        timeout=120,
    )
    print(f"statusCode={response.status_code}")
    print(response.text)


if __name__ == "__main__":
    main()
