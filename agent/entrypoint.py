"""AgentCore Runtime entrypoint for the Cala/Sayari router agent.

Copy this file (and core.py, requirements.txt in this same directory) into
the project folder that `agentcore create --protocol HTTP` scaffolds (or
into the zip for a direct-code deploy). Point agentcore.json's `entrypoint`
field at this file. See README.md "Deploy to AgentCore Runtime" in this
directory.

Unlike a2k-box's MCP entrypoint (../deploy/agentcore/entrypoint.py), this
uses the `bedrock_agentcore` SDK's BedrockAgentCoreApp, which implements
the /invocations (POST) and /ping (GET) contract AgentCore Runtime expects
for HTTP-protocol workloads -- no manual routing needed here.

Needs these set on the Runtime (see README):
    - Plain environment variables (not secret): GATEWAY_URL, BEDROCK_MODEL_ID
    - CLIENT_ID / CLIENT_SECRET: either as plain environment variables (fine for
      a quick test) or, preferably, via AGENT_SECRETS_ARN pointing at a Secrets
      Manager secret containing {"CLIENT_ID": "...", "CLIENT_SECRET": "..."} --
      see core.py's secret_env(). Runtime environment variables are visible to
      anyone with read access to the Runtime resource, unlike a Secrets Manager
      value gated by its own IAM policy.
Plus a `bedrock:InvokeModel*`/`bedrock:Converse*` permission on that model
for the Runtime's own execution role -- this agent calls Bedrock directly
(unlike a2k-box, which is purely a tool server with no model of its own) --
and, if using AGENT_SECRETS_ARN, `secretsmanager:GetSecretValue` scoped to
that secret's ARN.

Invocation payload contract: POST /invocations with {"prompt": "<question>"},
returns {"response": "<answer text>"}.

Observability: `context` (bedrock_agentcore.runtime.context.RequestContext) is
Runtime-injected metadata, not part of the payload -- the SDK detects the second
parameter by inspecting this function's signature (BedrockAgentCoreApp._takes_context)
and only passes it because it's declared here. `context.session_id` comes from
Runtime's own session header. There's no equivalent built-in notion of "which
internal system is calling us", so `internal_client` is read from a plain custom
header instead (`X-Internal-Client`) -- callers (K2, other internal services) are
expected to set it; falls back to "unknown" if they don't, same as
observability.emit_query_metrics does for the metrics dimension.
"""

from __future__ import annotations

import os

from bedrock_agentcore.runtime import BedrockAgentCoreApp
from bedrock_agentcore.runtime.context import RequestContext

from core import ask, secret_env

app = BedrockAgentCoreApp()

_INTERNAL_CLIENT_HEADER = "x-internal-client"


@app.entrypoint
def invoke(payload: dict, context: RequestContext) -> dict:
    question = payload.get("prompt", "")
    headers = context.request_headers or {}
    # Case-insensitive lookup rather than a fixed-case dict.get(): the SDK forwards
    # headers under whatever casing the request arrived with (see
    # bedrock_agentcore.runtime.app._build_request_context), and while a standard
    # ASGI stack normalises to lowercase, that's an assumption about what's in
    # front of this Runtime, not a guarantee this code should depend on silently.
    internal_client = next(
        (value for key, value in headers.items() if key.lower() == _INTERNAL_CLIENT_HEADER), "unknown"
    )
    answer = ask(
        question,
        gateway_url=os.environ["GATEWAY_URL"],
        client_id=secret_env("CLIENT_ID"),
        client_secret=secret_env("CLIENT_SECRET"),
        model_id=os.environ["BEDROCK_MODEL_ID"],
        region=os.environ.get("AWS_REGION", "eu-west-1"),
        silent=True,
        session_id=context.session_id,
        internal_client=internal_client,
    )
    return {"response": answer}


if __name__ == "__main__":
    app.run()
