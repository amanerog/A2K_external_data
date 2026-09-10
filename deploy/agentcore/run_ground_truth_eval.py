"""Runs a ground-truth CSV (id/query/expected_answer/source columns -- see
ground_truth_v4.csv, exported from GroundTruth_v4.numbers) against the
mcp_to_agent_to_mcp branch's deployed a2k-box Runtime: same `a2k.ask` MCP
call, same IAM/boto3 pattern as test_a2k_ask_via_agent.py in this directory.

`expected_answer` is free-form prose written by a human, and our system's
`answer` is free-form prose synthesized by an LLM -- these will essentially
never match verbatim even when the system is factually right (different
wording, different level of detail, different ordering of facts). So each
row is graded by a second LLM call (Bedrock Converse, "LLM-as-judge") that
compares actual vs. expected on *factual* consistency, not string equality.

Deliberately NOT scope-aware: the judge isn't told anything about this
system's vendor catalogue (Cala/Sayari) or which ground-truth rows a2k-box
can plausibly answer at all (a fair few of ground_truth_v4.csv's rows cite
sources -- EU-Lex, McKinsey, Eurostat, LinkedIn, etc. -- that aren't part of
the KB Cards catalogue today). Every row is graded the same way; a row whose
source Cala/Sayari simply don't cover will legitimately come back FAIL (or
the system will self-report insufficient evidence) -- that's expected, not
a bug in this script. Deciding which FAILs are "real" vs. "known out of
scope" is left to whoever reads the output CSV.

Usage:
    pip install boto3   # if not already present
    python run_ground_truth_eval.py
    python run_ground_truth_eval.py --input ground_truth_v4.csv --output results.csv
    python run_ground_truth_eval.py --limit 3                 # quick smoke run
    python run_ground_truth_eval.py --ids 71,73,75            # just these rows
    python run_ground_truth_eval.py --workers 3                # parallelize (each
                                                                # worker opens its own
                                                                # MCP session -- start
                                                                # conservative, AgentCore's
                                                                # real concurrency ceiling
                                                                # here isn't documented)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import boto3

from judge import DEFAULT_JUDGE_MODEL_ID, judge as _judge_call

REGION = "eu-west-1"
AGENT_RUNTIME_ARN = "arn:aws:bedrock-agentcore:eu-west-1:<ACCOUNT_ID>:runtime/a2k_external_data_mcp-A3c4F0Cyx7"
MCP_PROTOCOL_VERSION = "2025-06-18"


@dataclass
class GroundTruthRow:
    id: str
    query: str
    expected_answer: str
    source: str


@dataclass
class EvalResult:
    row: GroundTruthRow
    ok: bool = False
    actual_answer: Optional[str] = None
    verdict: str = "ERROR"
    rationale: str = ""
    latency_ms: float = 0.0
    error: str = ""


def _load_rows(path: Path, ids: Optional[set[str]], limit: Optional[int]) -> list[GroundTruthRow]:
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows = [
            GroundTruthRow(id=r["id"], query=r["query"], expected_answer=r["expected_answer"], source=r["source"])
            for r in reader
        ]
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
                    "clientInfo": {"name": "run_ground_truth_eval", "version": "1.0"},
                },
            }
        ).encode("utf-8"),
        contentType="application/json",
        accept="application/json, text/event-stream",
        mcpProtocolVersion=MCP_PROTOCOL_VERSION,
    )
    response["response"].read()  # drain -- we only need the session id from the response itself
    session_id = response.get("mcpSessionId")
    if not session_id:
        raise RuntimeError("initialize response carried no mcpSessionId")
    return session_id


def _extract_jsonrpc_result(body: str, expected_id: int) -> dict:
    """AgentCore streams SSE (`event: ...` / `data: ...` lines, plus bare
    `: ping - <timestamp>` comment lines while the tool call is still
    running -- see the raw output in this branch's manual test runs). The
    one line that matters is the `data: ` line whose JSON-RPC `id` matches
    the request we sent."""
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


def _call_a2k_ask(client, session_id: str, query: str) -> dict:
    """Calls a2k.ask for one query within an already-initialized MCP
    session. Returns the parsed CitedResponseEnvelope dict (or raises)."""
    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier="DEFAULT",
        payload=json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {"name": "a2k.ask", "arguments": {"query": query}},
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


def _evaluate_row(row: GroundTruthRow, mcp_client, bedrock_client, judge_model_id: str, session_id: str) -> EvalResult:
    result = EvalResult(row=row)
    t0 = time.monotonic()
    try:
        envelope = _call_a2k_ask(mcp_client, session_id, row.query)
        result.latency_ms = (time.monotonic() - t0) * 1000
        if not envelope.get("ok", False):
            result.actual_answer = None
            result.error = json.dumps(envelope.get("error"))
            result.verdict = "FAIL"
            result.rationale = "System returned ok=false -- see error column."
            result.ok = True  # the call itself succeeded; FAIL is a grading outcome, not a script error
            return result

        result.actual_answer = envelope.get("answer") or "(no answer -- insufficient evidence)"
        result.verdict, result.rationale = _judge_call(
            bedrock_client, judge_model_id, row.query, row.expected_answer, result.actual_answer
        )
        result.ok = True
    except Exception as exc:  # noqa: BLE001 -- one bad row must not kill the whole batch
        result.latency_ms = (time.monotonic() - t0) * 1000
        result.error = f"{type(exc).__name__}: {exc}"
        result.verdict = "ERROR"
    return result


def _run_sequential(rows: list[GroundTruthRow], judge_model_id: str) -> list[EvalResult]:
    mcp_client = boto3.client("bedrock-agentcore", region_name=REGION)
    bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)
    session_id = _new_mcp_session(mcp_client)

    results = []
    for i, row in enumerate(rows, 1):
        print(f"[{i}/{len(rows)}] id={row.id} {row.query[:70]!r}", flush=True)
        result = _evaluate_row(row, mcp_client, bedrock_client, judge_model_id, session_id)
        print(f"    -> {result.verdict} ({result.latency_ms:.0f}ms)" + (f"  ERROR: {result.error}" if result.error and result.verdict == "ERROR" else ""), flush=True)
        results.append(result)
    return results


def _run_parallel(rows: list[GroundTruthRow], judge_model_id: str, workers: int) -> list[EvalResult]:
    def _worker(chunk: list[GroundTruthRow]) -> list[EvalResult]:
        mcp_client = boto3.client("bedrock-agentcore", region_name=REGION)
        bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)
        session_id = _new_mcp_session(mcp_client)
        out = []
        for row in chunk:
            result = _evaluate_row(row, mcp_client, bedrock_client, judge_model_id, session_id)
            print(f"id={row.id} -> {result.verdict} ({result.latency_ms:.0f}ms)", flush=True)
            out.append(result)
        return out

    chunks: list[list[GroundTruthRow]] = [[] for _ in range(workers)]
    for i, row in enumerate(rows):
        chunks[i % workers].append(row)
    chunks = [c for c in chunks if c]

    results: list[EvalResult] = []
    with ThreadPoolExecutor(max_workers=len(chunks)) as pool:
        futures = [pool.submit(_worker, chunk) for chunk in chunks]
        for future in as_completed(futures):
            results.extend(future.result())
    return results


def _write_output(path: Path, results: list[EvalResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(
            ["id", "query", "expected_answer", "actual_answer", "verdict", "rationale", "latency_ms", "source", "error"]
        )
        for r in results:
            writer.writerow(
                [
                    r.row.id,
                    r.row.query,
                    r.row.expected_answer,
                    r.actual_answer or "",
                    r.verdict,
                    r.rationale,
                    f"{r.latency_ms:.0f}",
                    r.row.source,
                    r.error,
                ]
            )


def _print_summary(results: list[EvalResult]) -> None:
    counts: dict[str, int] = {}
    for r in results:
        counts[r.verdict] = counts.get(r.verdict, 0) + 1
    total = len(results)
    print("\n=== Summary ===")
    for verdict in ("PASS", "PARTIAL", "FAIL", "ERROR"):
        n = counts.get(verdict, 0)
        print(f"{verdict:8s} {n:3d}  ({n / total * 100:.0f}%)" if total else f"{verdict:8s} 0")
    print(f"{'TOTAL':8s} {total:3d}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", default="ground_truth_v4.csv", help="Ground-truth CSV path")
    parser.add_argument("--output", default="ground_truth_v4_results.csv", help="Where to write graded results")
    parser.add_argument("--ids", default=None, help="Comma-separated subset of ids to run")
    parser.add_argument("--limit", type=int, default=None, help="Only run the first N rows")
    parser.add_argument("--workers", type=int, default=1, help="Parallel MCP sessions (default: sequential)")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL_ID, help="Bedrock model id used to grade answers")
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

    print(f"Running {len(rows)} row(s) against {AGENT_RUNTIME_ARN} (judge model: {args.judge_model})")
    t0 = time.monotonic()
    if args.workers > 1:
        results = _run_parallel(rows, args.judge_model, args.workers)
    else:
        results = _run_sequential(rows, args.judge_model)
    elapsed = time.monotonic() - t0

    # Keep output in input order regardless of worker interleaving.
    order = {row.id: i for i, row in enumerate(rows)}
    results.sort(key=lambda r: order[r.row.id])

    output_path = Path(args.output)
    _write_output(output_path, results)
    print(f"\nWrote {len(results)} graded row(s) to {output_path} in {elapsed:.0f}s")
    _print_summary(results)


if __name__ == "__main__":
    main()
