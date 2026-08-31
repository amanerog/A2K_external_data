"""AgentCore Runtime entrypoint for the "mechanical" prompt experiment --
requested 2026-08-31 for a specific test of `core.SYSTEM_PROMPT_TEMPLATE_MECHANICAL`
(see that constant's own comment for what it deliberately drops vs. the normal
prompt: per-vendor query shaping and multi-vendor fan-out on ambiguity).

Same invocation shape as entrypoint.py, plus one addition: this prompt's own
"Inputs" section expects an optional `vendor` field alongside the question,
so this entrypoint also reads `payload.get("vendor")` (unlike entrypoint.py/
entrypoint_generic.py, which only ever read `prompt`) and passes it through
to `core.ask(vendor=...)`.

Invocation payload contract: POST /invocations with
{"prompt": "<question>", "vendor": "<optional sourceId>"}, returns
{"response": "<answer text>"}.
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
    vendor = payload.get("vendor")
    headers = context.request_headers or {}
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
        mechanical=True,
        vendor=vendor,
    )
    return {"response": answer}


if __name__ == "__main__":
    app.run()
