"""How long each vendor tool call takes, per tool (Cala's knowledge_search,
entity_search, ...), for a recorded evaluation run.

The agent's agent.tool_call log lines carry each call's start and end but
not the tool's name; the run's .jsonl carries each answer's toolCalls, named,
but no times. Both are written as each call finishes (observability.py's
ToolCallLogger appends to its list and logs in the same step), so for one
answer they come in the same order and are paired by position. Only
single-vendor answers whose two lists have the same length are used.

Each row also carries the question, the call's input (JSON) and its output
as readable text (the MCP result's nested JSON strings decoded, as the
admission judge reads it). The output is cut at --max-output-chars (32,000:
Excel holds at most 32,767 characters per cell; output_chars and
output_truncated say how long it really was). The CSV is ;-separated, UTF-8
with BOM, like the ground-truth files, so Excel opens it as is.

Usage (credentials for the account in the environment):
    python deploy/agentcore/tool_timings.py ../A2K_external_data/deploy/agentcore/routing_v7.jsonl \\
        --date 2026-10-09 --since 11:50 [--vendor cala] --output tool_timings_v7.csv

Writes <output> (one row per vendor tool call, with its question's total and
vendor time; a question with no call or no time per call gets one row and a
note) and <output>_preguntas.csv (one row per question).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
from collections import defaultdict
from datetime import datetime, timedelta, timezone

import sys
from pathlib import Path

import boto3

from latency_breakdown import AGENT_LOG_GROUP, BOX_LOG_GROUP, pick_run, query, ts, union_ms

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from a2k.cache.evidence import tool_output_text  # noqa: E402

# Not vendor calls: the agent's structured-output tool and the skills loader.
LOCAL_TOOLS = {"AskContent", "SearchContent", "skills"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("jsonl")
    parser.add_argument("--date", required=True, help="UTC day of the run, YYYY-MM-DD")
    parser.add_argument("--since", help="UTC time HH:MM on --date; earlier lines are ignored")
    parser.add_argument("--mode", default="shadow")
    parser.add_argument("--vendor", help="only this vendor's answers, e.g. cala")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    parser.add_argument("--output", default="tool_timings.csv")
    parser.add_argument("--max-output-chars", type=int, default=32000)
    parser.add_argument("--delimiter", default=";")
    args = parser.parse_args()

    rows = [json.loads(line) for line in open(args.jsonl, encoding="utf-8") if line.strip()]
    by_hash = {hashlib.sha256(r["query"].encode("utf-8")).hexdigest()[:16]: r for r in rows}
    day = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    start, end = day - timedelta(hours=2), day + timedelta(days=1, hours=2)
    logs = boto3.client("logs", region_name=args.region)

    retrievals = query(logs, BOX_LOG_GROUP, f'filter event = "cache.retrieval" and mode = "{args.mode}" | fields @timestamp, request_id, query_sha256', start, end)
    if args.since:
        since = datetime.fromisoformat(f"{args.date}T{args.since}").replace(tzinfo=timezone.utc)
        retrievals = [r for r in retrievals if ts(r["@timestamp"]) >= since]
    chosen = pick_run(retrievals, set(by_hash))
    lines = query(logs, AGENT_LOG_GROUP, 'filter event = "agent.tool_call" | fields session_id, tool_start_time, tool_end_time, error_type', start, end)
    by_session: dict[str, list[dict]] = defaultdict(list)
    for line in lines:
        by_session[line.get("session_id")].append(line)

    calls, questions, skipped = [], [], 0
    for h, r in by_hash.items():
        recorded = r.get("toolCalls") or []
        vendors = {tc.get("vendor") for tc in recorded if tc.get("vendor")}
        if args.vendor and vendors != {args.vendor}:
            continue
        base = {"id": r["id"], "question": r["query"], "question_total_ms": r.get("latencyMs")}
        logged = sorted(by_session.get(chosen[h]["request_id"], []), key=lambda x: x["tool_end_time"]) if h in chosen else []
        if not recorded:
            note = "no vendor call (answered without tools)" if r.get("answer") else f"no answer: {str(r.get('error') or '')[:120]}"
        elif h not in chosen:
            note = "not found in the logs of the run"
        elif len(logged) != len(recorded):
            note = f"logs ({len(logged)} calls) and record ({len(recorded)}) do not line up: no time per call"
        else:
            note = ""
        aligned = bool(recorded) and not note
        skipped += bool(recorded) and not aligned
        vendor_calls = []
        for i, tc in enumerate(recorded, start=1):
            if tc.get("toolName") in LOCAL_TOOLS:
                continue
            line = logged[i - 1] if aligned else None
            interval = (ts(line["tool_start_time"]), ts(line["tool_end_time"])) if line else None
            output = tool_output_text(tc.get("output"))
            vendor_calls.append({**base, "vendor": tc.get("vendor"), "order": i, "tool": tc.get("toolName"),
                                 "ms": round((interval[1] - interval[0]).total_seconds() * 1000) if interval else None,
                                 "_interval": interval, "status": tc.get("status"), "error": (line or {}).get("error_type") or tc.get("errorType") or "",
                                 "input": json.dumps(tc.get("input"), ensure_ascii=False),
                                 "output_chars": len(output), "output_truncated": len(output) > args.max_output_chars,
                                 "output": output[: args.max_output_chars], "note": note})
        intervals = [c["_interval"] for c in vendor_calls if c["_interval"]]
        vendor_ms = round(union_ms(intervals)) if intervals else None
        for c in vendor_calls:
            c["question_vendor_ms"] = vendor_ms
        calls += vendor_calls or [{**base, "vendor": "", "order": "", "tool": "", "ms": None, "_interval": None, "status": "",
                                   "error": "", "input": "", "output_chars": "", "output_truncated": "", "output": "",
                                   "question_vendor_ms": None, "note": note}]
        questions.append({**base, "vendors": ",".join(sorted(vendors)), "vendor_calls": len(vendor_calls),
                          "question_vendor_ms": vendor_ms, "note": note})

    with open(args.output, "w", newline="", encoding="utf-8-sig") as fh:
        fields = ["id", "question", "question_total_ms", "question_vendor_ms", "vendor", "order", "tool", "ms", "status", "error",
                  "input", "output_chars", "output_truncated", "output", "note"]
        w = csv.DictWriter(fh, fieldnames=fields, delimiter=args.delimiter, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(calls, key=lambda c: (int(c["id"]), c["order"] or 0)))
    per_question = str(Path(args.output).with_name(Path(args.output).stem + "_preguntas.csv"))
    with open(per_question, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=["id", "question", "vendors", "vendor_calls", "question_total_ms", "question_vendor_ms", "note"],
                           delimiter=args.delimiter)
        w.writeheader()
        w.writerows(sorted(questions, key=lambda q: int(q["id"])))

    calls = [c for c in calls if c["ms"] is not None]
    totals = sorted(q["question_total_ms"] / 1000 for q in questions if q["question_total_ms"])
    print(f"{len(questions)} questions; total time median {statistics.median(totals):.1f} s, p90 {totals[int(0.9 * (len(totals) - 1))]:.1f} s")
    print(f"{len(calls)} vendor tool calls timed, from {len({c['id'] for c in calls})} answers ({skipped} answers with calls but no time per call)\n")
    print(f"{'vendor/tool':38}{'calls':>6}{'median s':>10}{'p90 s':>8}{'mean s':>8}{'max s':>8}{'errors':>8}")
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for c in calls:
        groups[(c["vendor"], c["tool"])].append(c)
    for (vendor, tool), cs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
        ms = sorted(c["ms"] / 1000 for c in cs)
        print(f"{vendor + '/' + tool:38}{len(cs):>6}{statistics.median(ms):>10.1f}{ms[int(0.9 * (len(ms) - 1))]:>8.1f}"
              f"{statistics.mean(ms):>8.1f}{ms[-1]:>8.1f}{sum(c['status'] != 'success' for c in cs):>8}")
    per_answer = defaultdict(float)
    for c in calls:
        per_answer[c["id"]] += c["ms"] / 1000
    if per_answer:
        print(f"\nper answer: {statistics.mean(len([c for c in calls if c['id'] == i]) for i in per_answer):.1f} vendor calls, "
              f"{statistics.median(per_answer.values()):.1f} s summed (median; calls in parallel are counted each)")
    print(f"wrote {args.output} (one row per call) and {per_question} (one row per question)")


if __name__ == "__main__":
    main()
