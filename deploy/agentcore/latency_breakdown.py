"""Where the time of each answer goes: the vendor's tool calls against our
own work, for a recorded evaluation run (run_ground_truth_eval.py .jsonl),
from the CloudWatch logs of that run.

Per question:
  total            end to end, as the evaluation client measured it (latencyMs)
  vendor           the vendor's MCP tool calls (agent.tool_call start/end; the
                   union of the intervals, so parallel calls are not counted twice)
  agent_between    inside the span of the tool calls but outside them: the
                   agent's model deciding the next call
  agent_before     agent start -> first tool call: phase 1 (vendor choice) and
                   the model's first turn
  agent_after      last tool call -> agent end: the model writing the answer
  outside_agent    total - agent latency: a2k-box (token, envelope, claims,
                   citations), AgentCore Runtime and network, both hops
  cache_retrieval  the cache lookup (cache.retrieval total_ms); in shadow mode it
                   runs in parallel with the vendor call and adds nothing

Joins: question -> query_sha256 -> the cache.retrieval line of the run ->
request_id = the agent's session_id -> its agent.tool_call lines; the agent's
total comes from its query-metrics line (Latency), matched to the request by
log stream and time (the line carries no session id). agent_before/after and
outside_agent are only given when that match is found.

Usage (credentials for the account in the environment):
    python deploy/agentcore/latency_breakdown.py ../A2K_external_data/deploy/agentcore/routing_p1.jsonl \\
        --date 2026-10-08 [--mode shadow] --output latency_breakdown.csv
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import statistics
import time
from datetime import datetime, timedelta, timezone

import boto3

BOX_LOG_GROUP = "/aws/bedrock-agentcore/runtimes/a2k_cache_mcp-48ImkT2F9a-DEFAULT"
AGENT_LOG_GROUP = "/aws/bedrock-agentcore/runtimes/a2k_cache_agent-oEBeCrBECq-DEFAULT"
COLUMNS = ("total", "vendor", "agent_between", "agent_before", "agent_after", "outside_agent", "cache_retrieval")


def query(logs, group: str, text: str, start: datetime, end: datetime) -> list[dict[str, str]]:
    qid = logs.start_query(logGroupName=group, startTime=int(start.timestamp()), endTime=int(end.timestamp()), queryString=text, limit=10000)["queryId"]
    while (res := logs.get_query_results(queryId=qid))["status"] in ("Scheduled", "Running"):
        time.sleep(1)
    if res["status"] != "Complete":
        raise SystemExit(f"query on {group}: {res['status']}")
    return [{c["field"]: c["value"] for c in row} for row in res["results"]]


def ts(value: str) -> datetime:
    """Logs Insights' @timestamp ('2026-10-08 10:00:00.123') or an ISO time ('...T...Z')."""
    return datetime.fromisoformat(value.replace("Z", "+00:00").replace(" ", "T")).replace(tzinfo=timezone.utc)


def union_ms(intervals: list[tuple[datetime, datetime]]) -> float:
    total, current = 0.0, None
    for s, e in sorted(intervals):
        if current is None or s > current[1]:
            if current:
                total += (current[1] - current[0]).total_seconds()
            current = [s, e]
        else:
            current[1] = max(current[1], e)
    if current:
        total += (current[1] - current[0]).total_seconds()
    return total * 1000


def pick_run(retrievals: list[dict], hashes: set[str], hours: float = 3) -> dict[str, dict]:
    """The cache.retrieval line of each question in the run: the window of
    `hours` holding the most of the run's questions, first line per question."""
    events = sorted((r for r in retrievals if r.get("query_sha256") in hashes), key=lambda r: r["@timestamp"])
    best: tuple[int, int] = (0, 0)
    j = 0
    for i, e in enumerate(events):
        while ts(events[j]["@timestamp"]) < ts(e["@timestamp"]) - timedelta(hours=hours):
            j += 1
        n = len({x["query_sha256"] for x in events[j : i + 1]})
        if n > best[0]:
            best = (n, j)
    chosen: dict[str, dict] = {}
    start = ts(events[best[1]]["@timestamp"]) if events else None
    for e in events[best[1] :]:
        if start and ts(e["@timestamp"]) > start + timedelta(hours=hours):
            break
        chosen.setdefault(e["query_sha256"], e)
    return chosen


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("jsonl")
    parser.add_argument("--date", required=True, help="UTC day of the run, YYYY-MM-DD")
    parser.add_argument("--mode", default="shadow", help="cache mode of the run's cache.retrieval lines")
    parser.add_argument("--box-log-group", default=BOX_LOG_GROUP)
    parser.add_argument("--agent-log-group", default=AGENT_LOG_GROUP)
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    parser.add_argument("--output", default="latency_breakdown.csv")
    args = parser.parse_args()

    rows = [json.loads(line) for line in open(args.jsonl, encoding="utf-8") if line.strip()]
    by_hash = {hashlib.sha256(r["query"].encode("utf-8")).hexdigest()[:16]: r for r in rows}
    day = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    start, end = day - timedelta(hours=2), day + timedelta(days=1, hours=2)
    logs = boto3.client("logs", region_name=args.region)

    retrievals = query(logs, args.box_log_group,
                       f'filter event = "cache.retrieval" and mode = "{args.mode}" | fields @timestamp, request_id, query_sha256, total_ms', start, end)
    tool_calls = query(logs, args.agent_log_group,
                       'filter event = "agent.tool_call" | fields @timestamp, @logStream, session_id, tool_start_time, tool_end_time, error_type', start, end)
    agent_totals = query(logs, args.agent_log_group,
                         "filter ispresent(QueryCount) and ispresent(Latency) | fields @timestamp, @logStream, Latency, InvocationCount", start, end)
    chosen = pick_run(retrievals, set(by_hash))
    print(f"logs: {len(retrievals)} cache.retrieval, {len(tool_calls)} agent.tool_call, {len(agent_totals)} agent totals; "
          f"{len(chosen)}/{len(rows)} questions found in the run window")

    calls_by_session: dict[str, list[dict]] = {}
    for c in tool_calls:
        calls_by_session.setdefault(c.get("session_id"), []).append(c)
    totals_by_stream: dict[str, list[dict]] = {}
    for t in agent_totals:
        totals_by_stream.setdefault(t["@logStream"], []).append(t)
    used: set[int] = set()

    out = []
    for h, r in by_hash.items():
        rec = {"id": r["id"], "vendors": ",".join(sorted({tc["vendor"] for tc in r.get("toolCalls") or [] if tc.get("vendor")})),
               "total": r.get("latencyMs")}
        e = chosen.get(h)
        calls = calls_by_session.get(e["request_id"], []) if e else []
        rec["cache_retrieval"] = float(e["total_ms"]) if e and e.get("total_ms") else None
        rec["tool_calls"] = len(calls)
        if calls:
            intervals = [(ts(c["tool_start_time"]), ts(c["tool_end_time"])) for c in calls]
            first, last = min(s for s, _ in intervals), max(x for _, x in intervals)
            rec["vendor"] = union_ms(intervals)
            rec["agent_between"] = (last - first).total_seconds() * 1000 - rec["vendor"]
            # The agent's total: its query-metrics line, same log stream, the first after the last tool call.
            candidates = [t for t in totals_by_stream.get(calls[0]["@logStream"], []) if id(t) not in used
                          and timedelta(0) <= ts(t["@timestamp"]) - last <= timedelta(minutes=5)]
            if candidates:
                t = min(candidates, key=lambda t: ts(t["@timestamp"]))
                used.add(id(t))
                agent_ms = float(t["Latency"])
                agent_start = ts(t["@timestamp"]) - timedelta(milliseconds=agent_ms)
                rec["agent_before"] = max(0.0, (first - agent_start).total_seconds() * 1000)
                rec["agent_after"] = max(0.0, (ts(t["@timestamp"]) - last).total_seconds() * 1000)
                rec["outside_agent"] = rec["total"] - agent_ms if rec["total"] else None
        out.append(rec)

    fields = ["id", "vendors", "tool_calls", *COLUMNS]
    with open(args.output, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        for rec in sorted(out, key=lambda x: int(x["id"])):
            w.writerow({k: (round(rec[k] / 1000, 1) if k in COLUMNS and rec.get(k) is not None else rec.get(k)) for k in fields})

    complete = [x for x in out if x.get("outside_agent") is not None]
    with_vendor = [x for x in out if x.get("vendor") is not None]
    print(f"{len(with_vendor)} with vendor timings, {len(complete)} with the full split; seconds:")
    print(f"{'':16}{'median':>8}{'p90':>8}{'mean':>8}{'share':>8}")
    total_sum = sum(x["total"] for x in complete) or 1
    for k in COLUMNS:
        vals = [x[k] / 1000 for x in complete if x.get(k) is not None]
        if vals:
            share = f"{100 * sum(x[k] for x in complete if x.get(k) is not None) / total_sum:.0f}%" if k not in ("total", "cache_retrieval") else ""
            p90 = sorted(vals)[int(0.9 * (len(vals) - 1))]
            print(f"{k:16}{statistics.median(vals):>8.1f}{p90:>8.1f}{statistics.mean(vals):>8.1f}{share:>8}")
    for vendor in sorted({x["vendors"] for x in complete}):
        mine = [x for x in complete if x["vendors"] == vendor]
        print(f"  {vendor or 'no vendor'} ({len(mine)}): total {statistics.median(x['total'] for x in mine) / 1000:.1f}s, "
              f"vendor {statistics.median(x['vendor'] for x in mine) / 1000:.1f}s (median), "
              f"{100 * sum(x['vendor'] for x in mine) / sum(x['total'] for x in mine):.0f}% of the time in the vendor")
    print(f"wrote {args.output}")


if __name__ == "__main__":
    main()
