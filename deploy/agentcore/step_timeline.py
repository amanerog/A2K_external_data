"""Each answer of a recorded evaluation run, step by step: every call to the
model (agent.model_call: its time, tokens, stop reason, the tools it asked
for) and every vendor tool call (agent.tool_call: tool, time), in order --
plus, per question, where the time went. Needs the logs of agent v8 or later
(model_call lines; tool_name and duration_ms on tool_call lines).

Per question (<output>_preguntas.csv):
  total_ms          end to end, as the evaluation client measured it
  agent_ms          the agent's own total (its query-metrics line)
  model_ms          sum of its calls to the model, and how many (phase 1, the
                    vendor choice, apart: vendor_decision_ms)
  vendor_ms         the vendor tool calls (union: parallel calls count once)
  agent_other_ms    agent_ms - model - vendor: connecting to the vendor's MCP,
                    the agent's own code between steps
  outside_agent_ms  total_ms - agent_ms: a2k-box, AgentCore, network
  cache_retrieval_ms  the cache lookup (in shadow mode, in parallel: adds nothing)
Per step (<output>): question, order, kind (model / tool), phase or tool,
offset from the agent's first step, duration, tokens, stop reason, tools asked.

Usage (credentials for the account in the environment):
    python deploy/agentcore/step_timeline.py ../A2K_external_data/deploy/agentcore/routing_v7_cala.jsonl \\
        --date 2026-10-15 --since 09:30 --output pasos_gt_cala.csv
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
from pathlib import Path

import boto3

from latency_breakdown import AGENT_LOG_GROUP, BOX_LOG_GROUP, pick_run, query, ts, union_ms


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("jsonl")
    parser.add_argument("--date", required=True, help="UTC day of the run, YYYY-MM-DD")
    parser.add_argument("--since", help="UTC time HH:MM on --date; earlier lines are ignored")
    parser.add_argument("--mode", default="shadow")
    parser.add_argument("--region", default=os.environ.get("AWS_REGION", "eu-west-1"))
    parser.add_argument("--output", default="step_timeline.csv")
    parser.add_argument("--delimiter", default=";")
    args = parser.parse_args()

    rows = [json.loads(line) for line in open(args.jsonl, encoding="utf-8") if line.strip()]
    by_hash = {hashlib.sha256(r["query"].encode("utf-8")).hexdigest()[:16]: r for r in rows}
    day = datetime.fromisoformat(args.date).replace(tzinfo=timezone.utc)
    start, end = day - timedelta(hours=2), day + timedelta(days=1, hours=2)
    since = datetime.fromisoformat(f"{args.date}T{args.since}").replace(tzinfo=timezone.utc) if args.since else None
    logs = boto3.client("logs", region_name=args.region)

    retrievals = query(logs, BOX_LOG_GROUP, f'filter event = "cache.retrieval" and mode = "{args.mode}" | fields @timestamp, request_id, query_sha256, total_ms', start, end)
    if since:
        retrievals = [r for r in retrievals if ts(r["@timestamp"]) >= since]
    chosen = pick_run(retrievals, set(by_hash))
    model_lines = query(logs, AGENT_LOG_GROUP, 'filter event = "agent.model_call" | fields @logStream, session_id, phase, vendor, call, start_time, end_time, '
                        "duration_ms, bedrock_latency_ms, input_tokens, output_tokens, cache_read_tokens, stop_reason, error, "
                        # Logs Insights flattens a JSON array into name.0, name.1, ...
                        + ", ".join(f"tools_requested.{i}" for i in range(6)), start, end)
    tool_lines = query(logs, AGENT_LOG_GROUP, 'filter event = "agent.tool_call" | fields @logStream, session_id, vendor, tool_name, tool_start_time, tool_end_time, duration_ms, error_type', start, end)
    totals = query(logs, AGENT_LOG_GROUP, "filter ispresent(QueryCount) and ispresent(Latency) | fields @timestamp, @logStream, Latency", start, end)
    if not model_lines:
        raise SystemExit("no agent.model_call lines in that window -- the run needs agent v8 or later")

    steps_by_session: dict[str, list[dict]] = defaultdict(list)
    for m in model_lines:
        m["tools_requested"] = ",".join(m[f"tools_requested.{i}"] for i in range(6) if m.get(f"tools_requested.{i}"))
        steps_by_session[m.get("session_id")].append({"kind": "model", "name": m.get("phase"), "start": ts(m["start_time"]), "end": ts(m["end_time"]),
                                                      "stream": m.get("@logStream"), **{k: m.get(k) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "stop_reason", "tools_requested", "bedrock_latency_ms", "error")}})
    for t in tool_lines:
        if t.get("tool_name") in ("AskContent", "SearchContent", "skills"):
            continue  # local: the agent's structured output and skills loader
        steps_by_session[t.get("session_id")].append({"kind": "tool", "name": t.get("tool_name"), "vendor": t.get("vendor"), "start": ts(t["tool_start_time"]),
                                                      "end": ts(t["tool_end_time"]), "stream": t.get("@logStream"), "error": t.get("error_type")})
    totals_by_stream: dict[str, list[dict]] = defaultdict(list)
    for t in totals:
        totals_by_stream[t["@logStream"]].append(t)

    step_rows, question_rows = [], []
    for h, r in by_hash.items():
        base = {"id": r["id"], "question": r["query"]}
        e = chosen.get(h)
        steps = sorted(steps_by_session.get(e["request_id"], []) if e else [], key=lambda s: s["start"])
        error = r.get("error") or ("" if r.get("ok", True) else "not ok")
        q = {**base, "total_ms": r.get("latencyMs"), "cache_retrieval_ms": float(e["total_ms"]) if e and e.get("total_ms") else None,
             "note": f"the answer did not reach the client: {str(error)[:150]}" if error else ""}
        if not steps:
            q["note"] = "no steps in the logs (answered without the agent, or an error)"
            question_rows.append(q)
            continue
        first, last = steps[0]["start"], max(s["end"] for s in steps)
        for i, s in enumerate(steps, start=1):
            step_rows.append({**base, "order": i, "kind": s["kind"], "name": s["name"], "vendor": s.get("vendor") or "",
                              "offset_s": round((s["start"] - first).total_seconds(), 2), "duration_ms": round((s["end"] - s["start"]).total_seconds() * 1000),
                              **{k: s.get(k) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "bedrock_latency_ms", "stop_reason", "tools_requested", "error")}})
        models = [s for s in steps if s["kind"] == "model" and s["name"] == "answer"]
        decision = [s for s in steps if s["kind"] == "model" and s["name"] == "vendor_decision"]
        tools = [s for s in steps if s["kind"] == "tool"]
        ms = lambda ss: sum((s["end"] - s["start"]).total_seconds() * 1000 for s in ss)  # noqa: E731
        candidates = [t for t in totals_by_stream.get(steps[0]["stream"], []) if timedelta(0) <= ts(t["@timestamp"]) - last <= timedelta(minutes=5)]
        agent_ms = float(min(candidates, key=lambda t: ts(t["@timestamp"]))["Latency"]) if candidates else None
        vendor_ms = union_ms([(s["start"], s["end"]) for s in tools])
        q.update(agent_ms=agent_ms, vendor_decision_ms=round(ms(decision)), model_calls=len(models), model_ms=round(ms(models)),
                 model_input_tokens=sum(int(s.get("input_tokens") or 0) for s in models), model_output_tokens=sum(int(s.get("output_tokens") or 0) for s in models),
                 tool_calls=len(tools), vendor_ms=round(vendor_ms))
        if agent_ms is not None:
            q["agent_other_ms"] = round(agent_ms - ms(models) - ms(decision) - vendor_ms)
            q["outside_agent_ms"] = round(q["total_ms"] - agent_ms) if q["total_ms"] else None
        question_rows.append(q)

    with open(args.output, "w", newline="", encoding="utf-8-sig") as fh:
        fields = ["id", "question", "order", "kind", "name", "vendor", "offset_s", "duration_ms", "input_tokens", "output_tokens", "cache_read_tokens",
                  "bedrock_latency_ms", "stop_reason", "tools_requested", "error"]
        w = csv.DictWriter(fh, fieldnames=fields, delimiter=args.delimiter, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(step_rows, key=lambda s: (int(s["id"]), s["order"])))
    per_question = str(Path(args.output).with_name(Path(args.output).stem + "_preguntas.csv"))
    qfields = ["id", "question", "total_ms", "agent_ms", "vendor_decision_ms", "model_calls", "model_ms", "model_input_tokens", "model_output_tokens",
               "tool_calls", "vendor_ms", "agent_other_ms", "outside_agent_ms", "cache_retrieval_ms", "note"]
    with open(per_question, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=qfields, delimiter=args.delimiter, extrasaction="ignore")
        w.writeheader()
        w.writerows(sorted(question_rows, key=lambda q: int(q["id"])))

    complete = [q for q in question_rows if q.get("agent_ms") is not None and q.get("total_ms")]
    print(f"{len(question_rows)} questions, {len(complete)} with the full split, {len(step_rows)} steps\n")
    if complete:
        total = sum(q["total_ms"] for q in complete)
        print(f"{'seconds per question':24}{'median':>8}{'p90':>8}{'mean':>8}{'share':>8}")
        for k in ("total_ms", "vendor_decision_ms", "model_ms", "vendor_ms", "agent_other_ms", "outside_agent_ms", "cache_retrieval_ms"):
            vals = sorted(q[k] / 1000 for q in complete if q.get(k) is not None)
            if vals:
                share = "" if k in ("total_ms", "cache_retrieval_ms") else f"{100 * sum(q[k] for q in complete if q.get(k) is not None) / total:.0f}%"
                print(f"{k[:-3]:24}{statistics.median(vals):>8.1f}{vals[int(0.9 * (len(vals) - 1))]:>8.1f}{statistics.mean(vals):>8.1f}{share:>8}")
        print(f"\nmodel calls per answer: {statistics.mean(q['model_calls'] for q in complete):.1f}; "
              f"output tokens per answer (median): {statistics.median(q['model_output_tokens'] for q in complete):,.0f}")
    print(f"wrote {args.output} (one row per step) and {per_question} (one row per question)")


if __name__ == "__main__":
    main()
