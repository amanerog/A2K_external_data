"""Runs three preset questions through the router agent -- one crafted to
match Cala's declared coverage, one for Sayari's, one deliberately generic
(no clear topical match) -- and reports which `sources` value the model
actually passed to the ask tool for each, plus the final answer.

listVendors is no longer a tool the model can call at all (core.py fetches
it deterministically and injects the result into the system prompt instead
-- see that module's docstring "Routing" for why: it never reliably called
listVendors when that was left as its own choice). So a passing run here
should show exactly one ask call per question, `sources` reflecting the
catalogue printed at the top, never a listVendors call.

Must run the Strands agent loop in this process (not via
test_router_agent_iam.py's invoke_agent_runtime, which only returns the
deployed agent's final {"response": ...} text with no visibility into
intermediate tool calls) -- a custom callback_handler is the only way to
see the actual `sources` argument the model chose. core.py is the same code
either way (local or deployed), so this is a faithful proxy for the
deployed agent's routing behavior, not a different code path.

Usage:
    export CLIENT_ID=... CLIENT_SECRET=... GATEWAY_URL=... BEDROCK_MODEL_ID=...
    python test_routing_behavior.py
"""

from __future__ import annotations

import os

from strands import Agent
from strands.models import BedrockModel

from core import SYSTEM_PROMPT_TEMPLATE, _get_tools_and_catalogue

QUESTIONS = {
    "cala-leaning (SEC/OFAC filings)": (
        "What SEC/EDGAR filings and OFAC sanctions watchlist matches exist for Acme Robotics Inc.?"
    ),
    "sayari-leaning (ownership graph / adverse media)": (
        "What does the corporate ownership graph and adverse-media history look like for Acme Robotics Inc.?"
    ),
    "ambiguous (expect fan-out, no `sources` restriction)": ("Tell me everything you can find about Acme Robotics Inc."),
}


class ToolCallRecorder:
    """Minimal callback_handler: records each ask/search tool call's full
    input (name + arguments, `sources` in particular) by watching for
    Strands' ModelMessageEvent (message=...), which carries the complete
    assembled toolUse block -- unlike the raw streaming chunks
    PrintingCallbackHandler prints, which only ever show the tool *name*."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, **kwargs) -> None:
        message = kwargs.get("message")
        if not message:
            return
        for block in message.get("content", []):
            tool_use = block.get("toolUse")
            if tool_use:
                self.calls.append((tool_use["name"], tool_use.get("input", {})))


def main() -> None:
    gateway_url = os.environ["GATEWAY_URL"]
    client_id = os.environ["CLIENT_ID"]
    client_secret = os.environ["CLIENT_SECRET"]
    model_id = os.environ["BEDROCK_MODEL_ID"]
    region = os.environ.get("AWS_REGION", "eu-west-1")

    tools, catalogue_text = _get_tools_and_catalogue(gateway_url, client_id, client_secret)
    print(f"Vendor catalogue injected into the system prompt:\n{catalogue_text}\n")
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(vendor_catalogue=catalogue_text)
    model = BedrockModel(model_id=model_id, region_name=region)

    for label, question in QUESTIONS.items():
        recorder = ToolCallRecorder()
        agent = Agent(model=model, tools=tools, system_prompt=system_prompt, callback_handler=recorder)
        result = agent(question)

        print(f"\n{'=' * 70}\n{label}\nQ: {question}\n{'-' * 70}")
        for name, input_args in recorder.calls:
            print(f"  tool call: {name}({input_args})")
        print(f"{'-' * 70}\nA: {result}\n")


if __name__ == "__main__":
    main()
