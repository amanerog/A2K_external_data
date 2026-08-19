"""Same three questions as test_routing_behavior.py, but against the
*deployed* agent Runtime via boto3's invoke_agent_runtime (IAM/SigV4) --
confirms the same routing behavior holds end-to-end through the real
deployment, not just when running core.py locally.

Important difference from test_routing_behavior.py: invoke_agent_runtime
only returns the entrypoint's final {"response": "..."} text -- there is no
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
    python test_routing_behavior_deployed.py
"""

import json

import boto3

REGION = "eu-west-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:396961015428:runtime/a2k_agent-06B5R9CAuJ"

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
    client = boto3.client("bedrock-agentcore", region_name=REGION)

    for label, question in QUESTIONS.items():
        response = client.invoke_agent_runtime(
            agentRuntimeArn=AGENT_RUNTIME_ARN,
            qualifier="DEFAULT",
            payload=json.dumps({"prompt": question}).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
        body = json.loads(response["response"].read().decode("utf-8"))

        print(f"\n{'=' * 70}\n{label}\nQ: {question}\n{'-' * 70}")
        print(body.get("response", body))
        print()


if __name__ == "__main__":
    main()
