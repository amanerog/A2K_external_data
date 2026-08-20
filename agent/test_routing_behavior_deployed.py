"""Same three questions as test_routing_behavior.py, but against the
*deployed* agent Runtime via a JWT Bearer token over raw HTTPS (see
test_router_agent_jwt.py's docstring for why this Runtime can no longer be
called via boto3's invoke_agent_runtime -- its inbound authorizer was
switched from IAM to Cognito on 2026-08-19). Confirms the same routing
behavior holds end-to-end through the real deployment, not just when
running core.py locally.

Important difference from test_routing_behavior.py: this Runtime's
entrypoint only returns the final {"response": "..."} text -- there is no
intermediate tool-call trace to inspect the way a local callback_handler
gets one, so this can't directly confirm which `sources` value reached the
ask tool. Each question below explicitly asks the agent to state which
vendor(s) it consulted, and this script just prints the raw answers for you
to read -- a plausibility check, not a hard assertion the way
test_routing_behavior.py's ToolCallRecorder is. For an authoritative,
code-level check of a specific answer, take the `requestId` it mentions (if
it does) and look it up with a2k.getAuditRecord, or check a2k-box's own
CloudWatch logs (A2K_AUDIT_STDOUT=true logs every request including its
`sources`).

Usage:
    export AGENT_CLIENT_ID=... AGENT_USERNAME=... AGENT_PASSWORD=...
    python test_routing_behavior_deployed.py
"""

import urllib.parse

import httpx

from test_router_agent_jwt import REGION, AGENT_RUNTIME_ARN, _get_bearer_token

_ASK_FOR_SOURCE = (
    " At the end of your answer, on its own line, state exactly which vendor sourceId(s) "
    "you consulted (e.g. 'Sources used: cala' or 'Sources used: cala, sayari')."
)

QUESTIONS = {
    "cala-leaning (SEC/OFAC filings)": (
        "What SEC/EDGAR filings and OFAC sanctions watchlist matches exist for Acme Robotics Inc.?"
        + _ASK_FOR_SOURCE
    ),
    "sayari-leaning (ownership graph / adverse media)": (
        "What does the corporate ownership graph and adverse-media history look like for Acme Robotics Inc.?"
        + _ASK_FOR_SOURCE
    ),
    "ambiguous (expect fan-out, no `sources` restriction)": (
        "Tell me everything you can find about Acme Robotics Inc." + _ASK_FOR_SOURCE
    ),
}


def main() -> None:
    token = _get_bearer_token()
    encoded_arn = urllib.parse.quote(AGENT_RUNTIME_ARN, safe="")
    url = f"https://bedrock-agentcore.{REGION}.amazonaws.com/runtimes/{encoded_arn}/invocations?qualifier=DEFAULT"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }

    for label, question in QUESTIONS.items():
        response = httpx.post(url, headers=headers, json={"prompt": question}, timeout=120)
        body = response.json()

        print(f"\n{'=' * 70}\n{label}\nQ: {question}\n{'-' * 70}")
        print(body.get("response", body))
        print()


if __name__ == "__main__":
    main()
