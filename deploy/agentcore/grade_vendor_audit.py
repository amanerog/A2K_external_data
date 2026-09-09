"""Grades answers already captured by run_vendor_audit.py -- reads a
vendor_audit*.jsonl file (one record per (vendor, query) call, `answer`
already captured, no AgentCore call needed here) and a ground-truth CSV
(id/query/expected_answer/source -- see ground_truth_v4.csv), joins the two
on `id`, and grades each vendor's answer against `expected_answer` with the
same LLM-as-judge as run_ground_truth_eval.py (see judge.py -- shared, not
duplicated).

Unlike run_ground_truth_eval.py (which lets direct_agent.py's phase 1 pick
the vendor, or takes an explicit `sources` from the caller), this grades
each vendor's answer *in isolation* -- run_vendor_audit.py forced
`sources=["cala"]`/`["sayari"]` per call, so this tells you how good Cala
alone is and how good Sayari alone is on the same 35 questions, not just
how good the combined/routed system is.

A row with `ok=false` in the audit file (a failed call -- timeout, expired
credentials, etc., not a bad answer) is graded ERROR directly, no judge call
-- there's no answer to compare.

Usage:
    pip install boto3   # if not already present
    python grade_vendor_audit.py
    python grade_vendor_audit.py --audit vendor_audit_final.jsonl --ground-truth ground_truth_v4.csv
    python grade_vendor_audit.py --limit 5
    python grade_vendor_audit.py --workers 5   # judge calls are independent Bedrock
                                                # calls, not AgentCore MCP sessions --
                                                # safe to parallelize more aggressively
                                                # than run_ground_truth_eval.py's
                                                # --workers (see that script's own
                                                # caution about AgentCore concurrency)
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import boto3

from judge import DEFAULT_JUDGE_MODEL_ID, judge

REGION = "eu-west-1"


@dataclass
class AuditRecord:
    vendor: str
    id: str
    query: str
    ok: bool
    answer: Optional[str]
    error: str


@dataclass
class GradeResult:
    record: AuditRecord
    expected_answer: str
    verdict: str = "ERROR"
    rationale: str = ""


def _load_audit(path: Path, ids: Optional[set[str]], vendors: Optional[set[str]], limit: Optional[int]) -> list[AuditRecord]:
    records = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            records.append(
                AuditRecord(
                    vendor=r["vendor"],
                    id=str(r["id"]),
                    query=r["query"],
                    ok=r.get("ok", False),
                    answer=r.get("answer"),
                    error=r.get("error", ""),
                )
            )
    if ids:
        records = [r for r in records if r.id in ids]
    if vendors:
        records = [r for r in records if r.vendor in vendors]
    if limit:
        records = records[:limit]
    return records


def _load_expected_answers(path: Path) -> dict[str, str]:
    with path.open(encoding="utf-8") as f:
        return {row["id"]: row["expected_answer"] for row in csv.DictReader(f)}


def _grade_one(bedrock_client, model_id: str, record: AuditRecord, expected_answer: str) -> GradeResult:
    result = GradeResult(record=record, expected_answer=expected_answer)
    if not record.ok:
        result.verdict = "ERROR"
        result.rationale = f"Call failed, nothing to grade: {record.error}"
        return result
    actual_answer = record.answer or "(no answer -- insufficient evidence)"
    try:
        result.verdict, result.rationale = judge(bedrock_client, model_id, record.query, expected_answer, actual_answer)
    except Exception as exc:  # noqa: BLE001 -- one bad row must not kill the whole batch
        result.verdict = "ERROR"
        result.rationale = f"{type(exc).__name__}: {exc}"
    return result


def _write_output(path: Path, results: list[GradeResult]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["vendor", "id", "query", "expected_answer", "actual_answer", "ok", "verdict", "rationale"])
        for r in results:
            writer.writerow(
                [
                    r.record.vendor,
                    r.record.id,
                    r.record.query,
                    r.expected_answer,
                    r.record.answer or "",
                    r.record.ok,
                    r.verdict,
                    r.rationale,
                ]
            )


def _print_summary(results: list[GradeResult]) -> None:
    print("\n=== Summary ===")
    for vendor in sorted({r.record.vendor for r in results}):
        vendor_results = [r for r in results if r.record.vendor == vendor]
        counts: dict[str, int] = {}
        for r in vendor_results:
            counts[r.verdict] = counts.get(r.verdict, 0) + 1
        total = len(vendor_results)
        parts = "  ".join(f"{v}={counts.get(v, 0)}" for v in ("PASS", "PARTIAL", "FAIL", "ERROR"))
        print(f"{vendor:8s} {parts}   (total={total})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--audit", default="vendor_audit_final.jsonl", help="run_vendor_audit.py output .jsonl")
    parser.add_argument("--ground-truth", default="ground_truth_v4.csv", help="CSV with id/expected_answer columns")
    parser.add_argument("--output", default="vendor_audit_graded.csv", help="Where to write graded results")
    parser.add_argument("--vendors", default=None, help="Comma-separated subset of vendors, e.g. cala,sayari")
    parser.add_argument("--ids", default=None, help="Comma-separated subset of ids to grade")
    parser.add_argument("--limit", type=int, default=None, help="Only grade the first N records")
    parser.add_argument("--workers", type=int, default=5, help="Parallel judge calls (independent Bedrock calls -- safe to raise)")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL_ID, help="Bedrock model id used to grade answers")
    args = parser.parse_args()

    audit_path = Path(args.audit)
    gt_path = Path(args.ground_truth)
    if not audit_path.exists():
        print(f"error: {audit_path} not found", file=sys.stderr)
        sys.exit(1)
    if not gt_path.exists():
        print(f"error: {gt_path} not found", file=sys.stderr)
        sys.exit(1)

    ids = set(args.ids.split(",")) if args.ids else None
    vendors = set(v.strip() for v in args.vendors.split(",")) if args.vendors else None
    records = _load_audit(audit_path, ids, vendors, args.limit)
    if not records:
        print("error: no records selected (check --ids/--vendors/--limit)", file=sys.stderr)
        sys.exit(1)
    expected_answers = _load_expected_answers(gt_path)

    print(f"Grading {len(records)} record(s) from {audit_path} against {gt_path} (judge model: {args.judge_model})")
    bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)

    def _worker(record: AuditRecord) -> GradeResult:
        expected = expected_answers.get(record.id, "")
        result = _grade_one(bedrock_client, args.judge_model, record, expected)
        print(f"{record.vendor:6s} id={record.id:>3} -> {result.verdict}", flush=True)
        return result

    results: list[GradeResult] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(_worker, r): r for r in records}
        for future in as_completed(futures):
            results.append(future.result())

    # Keep output in a stable, readable order: vendor, then id as an int where possible.
    def _sort_key(r: GradeResult):
        try:
            id_key = int(r.record.id)
        except ValueError:
            id_key = r.record.id
        return (r.record.vendor, id_key)

    results.sort(key=_sort_key)

    output_path = Path(args.output)
    _write_output(output_path, results)
    print(f"\nWrote {len(results)} graded record(s) to {output_path}")
    _print_summary(results)


if __name__ == "__main__":
    main()
