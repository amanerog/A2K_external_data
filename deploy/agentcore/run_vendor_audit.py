"""Runs a ground-truth CSV's queries against a2k-box's deployed Runtime --
same `a2k.ask` MCP call, same IAM/boto3 pattern as test_a2k_ask_via_agent.py
-- once per vendor, `sources` forced explicitly (skips direct_agent.py's own
phase-1 vendor decision entirely, see engine.py's `_call_agent`/`handle()`:
an explicit `sources` list always short-circuits it). Default order is every
row against Cala first, then every row against Sayari -- not interleaved --
so a partial/interrupted run still gives one vendor's complete picture.

Unlike run_ground_truth_eval.py, this is NOT a pass/fail grading run -- no
LLM-as-judge, no comparison to `expected_answer`. It's an audit/
instrumentation report: for every call, was it ok, how many input/output
tokens did our agent's own Bedrock calls burn (CitedResponseEnvelope's
`usage.inputTokens`/`outputTokens`/`totalTokens` -- see a2k/models/
envelope.py and agent/direct_agent.py's handle()), and exactly which
vendor-MCP tools got called with what input and what came back
(`toolCalls[]`, one entry per call, from agent/observability.py's
ToolCallLogger.calls).

Two outputs:
  - <output>.jsonl       full detail, one JSON object per (vendor, row) call,
                          including every tool call's actual input/output.
  - <output>_summary.csv flat summary for a quick scan/pivot: one row per
                          call with ok/tokens/tool_call_count/tool_names,
                          no nested detail (that's what the .jsonl is for).

Usage:
    pip install boto3   # if not already present
    python run_vendor_audit.py
    python run_vendor_audit.py --input ground_truth_v4.csv --output vendor_audit
    python run_vendor_audit.py --vendors cala          # just one vendor
    python run_vendor_audit.py --limit 5               # quick smoke run
    python run_vendor_audit.py --ids 71,73,75
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import boto3

REGION = "eu-west-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:<ACCOUNT_ID>:runtime/a2k_external_data_mcp-A3c4F0Cyx7"
MCP_PROTOCOL_VERSION = "2025-06-18"


@dataclass
class GroundTruthRow:
    id: str
    query: str


@dataclass
class AuditResult:
    vendor: str
    row: GroundTruthRow
    ok: bool = False
    answer: Optional[str] = None
    input_tokens: Optional[int] = None
    output_tokens: Optional[int] = None
    total_tokens: Optional[int] = None
    latency_ms: Optional[int] = None
    tool_calls: list[dict] = None  # type: ignore[assignment]
    error: str = ""

    def __post_init__(self):
        if self.tool_calls is None:
            self.tool_calls = []


def _load_rows(path: Path, ids: Optional[set[str]], limit: Optional[int]) -> list[GroundTruthRow]:
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [GroundTruthRow(id=r["id"], query=r["query"]) for r in reader]
    if ids:
        rows = [r for r in rows if r.id in ids]
    if limit:
        rows = rows[:limit]
    return rows


def _new_mcp_session(client) -> str:
    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier="DEFAULT",
        payload=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": MCP_PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {"name": "run_vendor_audit", "version": "1.0"},
                },
            }
        ).encode("utf-8"),
        contentType="application/json",
        accept="application/json, text/event-stream",
        mcpProtocolVersion=MCP_PROTOCOL_VERSION,
    )
    response["response"].read()
    session_id = response.get("mcpSessionId")
    if not session_id:
        raise RuntimeError("initialize response carried no mcpSessionId")
    return session_id


def _extract_jsonrpc_result(body: str, expected_id: int) -> dict:
    """See run_ground_truth_eval.py's identical helper -- same SSE shape,
    same reasoning (ping comment lines mixed in while the call is still
    running; only the `data: ` line whose id matches ours matters)."""
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        try:
            obj = json.loads(line[len("data: ") :])
        except json.JSONDecodeError:
            continue
        if obj.get("id") == expected_id:
            return obj
    raise RuntimeError(f"no JSON-RPC response with id={expected_id} in AgentCore response body")


def _call_a2k_ask(client, session_id: str, query: str, sources: list[str]) -> dict:
    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier="DEFAULT",
        payload=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "a2k.ask", "arguments": {"query": query, "sources": sources}},
            }
        ).encode("utf-8"),
        contentType="application/json",
        accept="application/json, text/event-stream",
        mcpProtocolVersion=MCP_PROTOCOL_VERSION,
        mcpSessionId=session_id,
    )
    status_code = response["statusCode"]
    body = response["response"].read().decode("utf-8")
    if status_code != 200:
        raise RuntimeError(f"invoke_agent_runtime statusCode={status_code}: {body[:500]}")

    rpc_result = _extract_jsonrpc_result(body, expected_id=3)
    if "error" in rpc_result:
        raise RuntimeError(f"MCP error: {rpc_result['error']}")
    result = rpc_result["result"]
    if result.get("isError"):
        raise RuntimeError(f"a2k.ask tool error: {result}")

    text = result["content"][0]["text"]
    return json.loads(text)


def _run_one(client, session_id: str, vendor: str, row: GroundTruthRow) -> AuditResult:
    result = AuditResult(vendor=vendor, row=row)
    t0 = time.monotonic()
    try:
        envelope = _call_a2k_ask(client, session_id, row.query, sources=[vendor])
        result.ok = bool(envelope.get("ok", False))
        result.answer = envelope.get("answer")
        usage = envelope.get("usage") or {}
        result.input_tokens = usage.get("inputTokens")
        result.output_tokens = usage.get("outputTokens")
        result.total_tokens = usage.get("totalTokens")
        result.latency_ms = usage.get("latencyMs")
        result.tool_calls = envelope.get("toolCalls") or []
        if not result.ok:
            result.error = json.dumps(envelope.get("error"))
    except Exception as exc:  # noqa: BLE001 -- one bad row must not kill the whole batch
        result.error = f"{type(exc).__name__}: {exc}"
    if result.latency_ms is None:
        result.latency_ms = round((time.monotonic() - t0) * 1000)
    return result


def _write_jsonl(path: Path, results: list[AuditResult]) -> None:
    with path.open("w", encoding="utf-8") as f:
        for r in results:
            f.write(
                json.dumps(
                    {
                        "vendor": r.vendor,
                        "id": r.row.id,
                        "query": r.row.query,
                        "ok": r.ok,
                        "answer": r.answer,
                        "inputTokens": r.input_tokens,
                        "outputTokens": r.output_tokens,
                        "totalTokens": r.total_tokens,
                        "latencyMs": r.latency_ms,
                        "toolCalls": r.tool_calls,
                        "error": r.error,
                    },
                    default=str,
                    ensure_ascii=False,
                )
                + "\n"
            )


def _write_summary_csv(path: Path, results: list[AuditResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            [
                "vendor",
                "id",
                "query",
                "ok",
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "latency_ms",
                "tool_call_count",
                "tool_names",
                "error",
            ]
        )
        for r in results:
            tool_names = ", ".join(tc.get("toolName", "") for tc in r.tool_calls)
            writer.writerow(
                [
                    r.vendor,
                    r.row.id,
                    r.row.query,
                    r.ok,
                    r.input_tokens,
                    r.output_tokens,
                    r.total_tokens,
                    r.latency_ms,
                    len(r.tool_calls),
                    tool_names,
                    r.error,
                ]
            )


def _print_summary(results: list[AuditResult]) -> None:
    print("\n=== Summary ===")
    for vendor in sorted({r.vendor for r in results}):
        vendor_results = [r for r in results if r.vendor == vendor]
        ok_count = sum(1 for r in vendor_results if r.ok)
        total_in = sum(r.input_tokens or 0 for r in vendor_results)
        total_out = sum(r.output_tokens or 0 for r in vendor_results)
        total_calls = sum(len(r.tool_calls) for r in vendor_results)
        print(
            f"{vendor:8s} ok={ok_count}/{len(vendor_results)}  "
            f"inputTokens={total_in}  outputTokens={total_out}  toolCalls={total_calls}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="ground_truth_v4.csv", help="CSV with id/query columns")
    parser.add_argument("--output", default="vendor_audit", help="Output basename (writes <output>.jsonl and <output>_summary.csv)")
    parser.add_argument("--vendors", default="cala,sayari", help="Comma-separated vendors, run in this order")
    parser.add_argument("--ids", default=None, help="Comma-separated subset of ids to run")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N rows (per vendor)")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"error: {input_path} not found", file=sys.stderr)
        sys.exit(1)

    ids = set(args.ids.split(",")) if args.ids else None
    rows = _load_rows(input_path, ids, args.limit)
    if not rows:
        print("error: no rows selected (check --ids/--limit)", file=sys.stderr)
        sys.exit(1)
    vendors = [v.strip() for v in args.vendors.split(",") if v.strip()]

    print(f"Running {len(rows)} row(s) x {len(vendors)} vendor(s) = {len(rows) * len(vendors)} call(s) against {AGENT_RUNTIME_ARN}")
    client = boto3.client("bedrock-agentcore", region_name=REGION)

    all_results: list[AuditResult] = []
    t0 = time.monotonic()
    for vendor in vendors:
        print(f"\n--- vendor={vendor} ---")
        session_id = _new_mcp_session(client)
        for i, row in enumerate(rows, 1):
            print(f"[{i}/{len(rows)}] id={row.id} {row.query[:70]!r}", flush=True)
            result = _run_one(client, session_id, vendor, row)
            status = "OK" if result.ok else "FAIL"
            print(
                f"    -> {status}  in={result.input_tokens} out={result.output_tokens} "
                f"tools={len(result.tool_calls)} ({result.latency_ms}ms)"
                + (f"  ERROR: {result.error}" if result.error and not result.ok else ""),
                flush=True,
            )
            all_results.append(result)
    elapsed = time.monotonic() - t0

    jsonl_path = Path(f"{args.output}.jsonl")
    summary_path = Path(f"{args.output}_summary.csv")
    _write_jsonl(jsonl_path, all_results)
    _write_summary_csv(summary_path, all_results)

    print(f"\nWrote {len(all_results)} call(s) in {elapsed:.0f}s to:")
    print(f"  {jsonl_path}  (full detail, incl. each tool call's input/output)")
    print(f"  {summary_path}  (flat summary)")
    _print_summary(all_results)


if __name__ == "__main__":
    main()
