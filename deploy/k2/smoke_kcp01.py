"""A2K-KCP 0.1 smoke test of a deployed a2k-box Runtime, the way K2 calls it:
MCP over AgentCore's InvokeAgentRuntime (IAM), the 0.1 request as tool
arguments, the 0.1 envelope in structuredContent. Every envelope and the KB
Card are validated against the reference package's JSON Schemas.

Checks: a2k://card, tools/list (only a2k.ask and a2k.search, with their
outputSchema), an answered ask, a search, and three failures settled before
answering (wrong version, strict grounding, a cursor).

Usage:
    export A2K_BOX_RUNTIME_ARN=arn:aws:bedrock-agentcore:eu-west-1:...:runtime/a2k_cache_mcp-...
    python deploy/k2/smoke_kcp01.py --a2k-repo ../sgt-ap11702-a2k-main [--query "..."]
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

import boto3
from botocore.config import Config as BotoConfig

MCP_PROTOCOL_VERSION = "2025-06-18"


class Session:
    def __init__(self, arn: str, region: str):
        self.arn = arn
        self.client = boto3.client("bedrock-agentcore", region_name=region, config=BotoConfig(read_timeout=900, retries={"max_attempts": 1}))
        self.next_id = 1
        self.session_id = None
        self.rpc("initialize", {"protocolVersion": MCP_PROTOCOL_VERSION, "capabilities": {}, "clientInfo": {"name": "smoke_kcp01", "version": "1.0"}})

    def rpc(self, method: str, params: dict) -> dict:
        self.next_id += 1
        kwargs = dict(
            agentRuntimeArn=self.arn,
            qualifier="DEFAULT",
            payload=json.dumps({"jsonrpc": "2.0", "id": self.next_id, "method": method, "params": params}).encode("utf-8"),
            contentType="application/json",
            accept="application/json, text/event-stream",
            mcpProtocolVersion=MCP_PROTOCOL_VERSION,
        )
        if self.session_id:
            kwargs["mcpSessionId"] = self.session_id
        response = self.client.invoke_agent_runtime(**kwargs)
        self.session_id = self.session_id or response.get("mcpSessionId")
        body = response["response"].read().decode("utf-8")
        for line in body.splitlines():
            text = line[len("data: "):] if line.startswith("data: ") else line
            try:
                obj = json.loads(text)
            except json.JSONDecodeError:
                continue
            if obj.get("id") == self.next_id:
                if "error" in obj:
                    raise RuntimeError(f"{method}: JSON-RPC error {obj['error']}")
                return obj["result"]
        raise RuntimeError(f"{method}: no JSON-RPC response in {body[:300]!r}")

    def call(self, tool: str, arguments: dict) -> tuple[dict, float]:
        t0 = time.monotonic()
        result = self.rpc("tools/call", {"name": tool, "arguments": arguments})
        if result.get("isError"):
            raise RuntimeError(f"{tool}: isError true -- an A2K envelope is never isError: {result}")
        if "structuredContent" in result:  # a2k.ask / a2k.search
            return result["structuredContent"], time.monotonic() - t0
        # The internal.* tools return their result as JSON text only.
        return json.loads(result["content"][0]["text"]), time.monotonic() - t0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--a2k-repo", type=Path, help="clone of sgt-ap11702-a2k, if the a2k package is not installed")
    parser.add_argument("--runtime-arn", default=os.environ.get("A2K_BOX_RUNTIME_ARN"))
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    parser.add_argument("--query", default="¿Qué responsabilidades atribuye la EBA al órgano de administración respecto a la cultura de riesgos?")
    args = parser.parse_args()
    if not args.runtime_arn:
        sys.exit("set A2K_BOX_RUNTIME_ARN or --runtime-arn")
    if args.a2k_repo:
        sys.path.insert(0, str(args.a2k_repo / "src"))
    from a2k_kbcard.schema.validation import SchemaValidationError, validate_by_name

    failures = 0

    def check(label: str, ok: bool, detail: str = "") -> None:
        nonlocal failures
        failures += not ok
        print(f"{'PASS' if ok else 'FAIL'}  {label}{(' -- ' + detail) if detail else ''}", flush=True)

    def valid(envelope: dict, schema: str) -> bool:
        try:
            validate_by_name(envelope, schema)
            return True
        except SchemaValidationError as exc:
            print(f"      {schema}: {exc.errors[0]['instancePath'] or '/'}: {exc.errors[0]['message']}")
            return False

    session = Session(args.runtime_arn, args.region)

    card = json.loads(session.rpc("resources/read", {"uri": "a2k://card"})["contents"][0]["text"])
    check("a2k://card is a valid 0.1 KB Card", valid(card, "kb-card"), card.get("id", ""))

    tools = {t["name"]: t for t in session.rpc("tools/list", {})["tools"]}
    a2k_tools = sorted(n for n in tools if n.startswith("a2k."))
    check("tools: a2k.ask and a2k.search only", a2k_tools == ["a2k.ask", "a2k.search"], ", ".join(sorted(tools)))
    check("outputSchema declared", all((tools.get(n, {}).get("outputSchema") or {}).get("title", "").startswith("A2K 0.1") for n in a2k_tools))

    def request(operation: str, **extra) -> dict:
        return {"a2kVersion": "0.1", "operation": operation, "requestId": f"smoke-{uuid.uuid4().hex[:8]}", "query": args.query, **extra}

    out, seconds = session.call("a2k.ask", request("ask"))
    data = out.get("data", {})
    check("ask: valid 0.1 envelope", valid(out, "ask-response"), f"{seconds:.1f}s, outcome={data.get('outcome')}, "
          f"citations={len(data.get('citations', []))}, grounding={data.get('grounding', {}).get('status')}")
    if data.get("answer"):
        print("      " + data["answer"][:300].replace("\n", " ") + ("..." if len(data["answer"]) > 300 else ""))

    out, seconds = session.call("a2k.search", request("search", pagination={"limit": 5}))
    check("search: valid 0.1 envelope", valid(out, "search-response"), f"{seconds:.1f}s, passages={len(out.get('data', {}).get('passages', []))}")

    for label, tool, arguments, code in (
        ("wrong version", "a2k.ask", request("ask", a2kVersion="0.2"), "UNSUPPORTED_VERSION"),
        ("strict grounding", "a2k.ask", request("ask", requirements={"features": ["strict-grounding"]}), "REQUIRED_FEATURE_UNSUPPORTED"),
        ("cursor", "a2k.search", request("search", pagination={"cursor": "x"}), "PAGINATION_CURSOR_INVALID"),
    ):
        out, _ = session.call(tool, arguments)
        got = (out.get("error") or {}).get("code")
        check(f"{label} -> {code}", got == code and valid(out, f"{tool.split('.')[1]}-response"), str(got))

    print(f"\n{'ALL PASS' if not failures else f'{failures} FAILED'}")
    sys.exit(1 if failures else 0)


if __name__ == "__main__":
    main()
