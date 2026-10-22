"""Judge precision per domain (go / no-go gate 3, dashboard task T2.2): of the
answers the admission judge let into the cache (ACTIVE), the share the
ground-truth evaluation graded ACCEPTABLE.

Only a ground-truth run knows the expected answers, so this runs after one:
run_ground_truth_eval.py with A2K_BOX_RUNTIME_ARN pointing at the cache
Runtime (a2k_cache_mcp) asks every question through a2k-box, which admits
the vendor answers in the background. This script joins the run's graded CSV
with the cache.admission lines of the same window on query_sha256 (the hash
of the question exactly as sent, a2k/logline.py) and writes one
cache.ground_truth line per domain to the log group the go / no-go table
reads (GroundTruthLogGroup in cache-dashboard.yaml).

A question with no admission in the window is not counted: a would-be hit
(no candidate is created), a vendor error, or an admission still running.
`JudgeEvaluated` is how many ACTIVE answers the precision rests on -- a
domain with a handful is not evidence either way. `JudgeMissedGood` counts
the ACCEPTABLE answers the judge did not admit (what its strictness costs).

Usage (credentials for the account in the environment), right after the run:
    python3 deploy/observability/publish_judge_precision.py \\
        --results ground_truth_pocv3_results.csv --hours 6 [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Run from anywhere: put the repo root (two levels up from deploy/observability/)
# on sys.path so `import a2k...` works without installing the package first.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from a2k.logline import query_hash  # noqa: E402

BOX_LOG_GROUP = "/aws/bedrock-agentcore/runtimes/a2k_cache_mcp-48ImkT2F9a-DEFAULT"
GROUND_TRUTH_LOG_GROUP = "/a2k/cache/ground-truth"
GRADED = ("ACCEPTABLE", "NOT_ACCEPTABLE")  # PENDING_MANUAL_REVIEW and ERROR say nothing about the judge

ADMISSIONS_QUERY = (
    'filter event = "cache.admission" '
    "| fields @timestamp, query_sha256, entry_id, outcome, domains.0 as domain "
    "| sort @timestamp asc | limit 10000"
)


def load_verdicts(path: Path, column: str) -> dict[str, str]:
    """query_sha256 -> verdict, from a run_ground_truth_eval.py results CSV."""
    with path.open(encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if column not in (reader.fieldnames or []) or "query" not in (reader.fieldnames or []):
            sys.exit(f"{path}: needs 'query' and {column!r} columns, has {reader.fieldnames}")
        return {query_hash(row["query"]): (row[column] or "").strip().upper() for row in reader if row["query"]}


def latest_admissions(rows: list[dict[str, str]]) -> dict[str, dict[str, str]]:
    """query_sha256 -> its latest admission (rows oldest first): a question
    asked twice in the window is judged on the last answer."""
    return {r["query_sha256"]: r for r in rows if r.get("query_sha256")}


def judge_precision(verdicts: dict[str, str], admissions: dict[str, dict[str, str]]) -> tuple[dict[str, dict[str, int]], int]:
    """Per domain: active (ACTIVE and graded), active_ok (of those, ACCEPTABLE),
    missed_good (ACCEPTABLE, not admitted). Also how many questions matched an admission."""
    per_domain: dict[str, dict[str, int]] = {}
    matched = 0
    for qhash, verdict in verdicts.items():
        adm = admissions.get(qhash)
        if adm is None:
            continue
        matched += 1
        if verdict not in GRADED:
            continue
        d = per_domain.setdefault(adm.get("domain") or "NONE", {"active": 0, "active_ok": 0, "missed_good": 0})
        if adm.get("outcome") == "ACTIVE":
            d["active"] += 1
            d["active_ok"] += verdict == "ACCEPTABLE"
        elif verdict == "ACCEPTABLE":
            d["missed_good"] += 1
    return per_domain, matched


def ground_truth_lines(per_domain: dict[str, dict[str, int]], run: str) -> list[dict[str, Any]]:
    """The cache.ground_truth lines the go / no-go table reads. No precision
    for a domain with no ACTIVE answer graded."""
    lines = []
    for domain, d in sorted(per_domain.items()):
        line: dict[str, Any] = {"event": "cache.ground_truth", "Domain": domain, "GroundTruthRun": run, "JudgeEvaluated": d["active"], "JudgeMissedGood": d["missed_good"]}
        if d["active"]:
            line["JudgePrecisionPct"] = round(100 * d["active_ok"] / d["active"], 1)
        lines.append(line)
    return lines


def query_admissions(logs, log_group: str, start: datetime, end: datetime) -> list[dict[str, str]]:
    qid = logs.start_query(logGroupNames=[log_group], startTime=int(start.timestamp()), endTime=int(end.timestamp()), queryString=ADMISSIONS_QUERY)["queryId"]
    while (res := logs.get_query_results(queryId=qid))["status"] in ("Scheduled", "Running"):
        time.sleep(1)
    if res["status"] != "Complete":
        sys.exit(f"Logs Insights query {res['status']}")
    return [{c["field"]: c["value"] for c in row} for row in res["results"]]


def put_lines(logs, log_group: str, run: str, lines: list[dict[str, Any]]) -> str:
    stream = f"{run}-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    logs.create_log_stream(logGroupName=log_group, logStreamName=stream)
    now = int(time.time() * 1000)
    logs.put_log_events(logGroupName=log_group, logStreamName=stream, logEvents=[{"timestamp": now, "message": json.dumps(line)} for line in lines])
    return stream


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--results", required=True, type=Path, help="run_ground_truth_eval.py results CSV")
    parser.add_argument("--verdict-column", default="verdict", help="verdict (default) or final_verdict")
    parser.add_argument("--hours", type=float, default=24, help="the run happened within the last N hours")
    parser.add_argument("--run", default=None, help="label of the run (default: today's date)")
    parser.add_argument("--box-log-group", default=BOX_LOG_GROUP)
    parser.add_argument("--ground-truth-log-group", default=GROUND_TRUTH_LOG_GROUP)
    parser.add_argument("--region", default=None)
    parser.add_argument("--dry-run", action="store_true", help="print the lines, do not write them")
    args = parser.parse_args()

    import boto3

    verdicts = load_verdicts(args.results, args.verdict_column)
    logs = boto3.session.Session(region_name=args.region or os.environ.get("AWS_REGION") or "eu-west-1").client("logs")
    end = datetime.now(timezone.utc)
    admissions = latest_admissions(query_admissions(logs, args.box_log_group, end - timedelta(hours=args.hours), end))
    per_domain, matched = judge_precision(verdicts, admissions)
    run = args.run or f"{end:%Y-%m-%d}"
    lines = ground_truth_lines(per_domain, run)

    print(f"{len(verdicts)} questions in {args.results.name}, {matched} with an admission in the last {args.hours:g} h")
    active = sum(d["active"] for d in per_domain.values())
    active_ok = sum(d["active_ok"] for d in per_domain.values())
    print(f"all domains: {active_ok}/{active} ACTIVE answers ACCEPTABLE" + (f" = {100 * active_ok / active:.1f} %" if active else ""))
    for line in lines:
        print("  " + json.dumps(line))
    if not lines:
        sys.exit("nothing to publish: no graded question matched an admission (wrong window, or the run went to another Runtime?)")
    if args.dry_run:
        print("dry run: nothing written")
        return
    stream = put_lines(logs, args.ground_truth_log_group, run, lines)
    print(f"written to {args.ground_truth_log_group} / {stream}")


if __name__ == "__main__":
    main()
