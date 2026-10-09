"""Runs every widget of cache-dashboard.yaml against the account, the way the
dashboard would: each metric widget through GetMetricData (its SEARCH and
math expressions), each table through Logs Insights. Prints, per widget, the
series or rows it got back, or the error -- so a widget that parses but shows
nothing is told apart from one that is broken.

Usage (credentials for the account in the environment):
    python deploy/observability/check_dashboard.py [--hours 3] \\
        [--box-log-group ...] [--agent-log-group ...]
"""

from __future__ import annotations

import argparse
import json
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import boto3

TEMPLATE = Path(__file__).with_name("cache-dashboard.yaml")


def template_body_and_defaults() -> tuple[str, dict[str, str]]:
    text = TEMPLATE.read_text(encoding="utf-8")
    defaults = dict(re.findall(r"\n  (\w+):\n    Type: String\n    Default: (\S+)", text))
    block = text.split("DashboardBody: !Sub |\n", 1)[1]
    lines = []
    for line in block.splitlines():
        if line and not line.startswith("        "):
            break
        lines.append(line[8:])
    return "\n".join(lines), defaults


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--hours", type=float, default=3)
    parser.add_argument("--region", default=None)
    parser.add_argument("--box-log-group")
    parser.add_argument("--agent-log-group")
    args = parser.parse_args()

    body, params = template_body_and_defaults()
    session = boto3.session.Session(region_name=args.region)
    params["AWS::Region"] = session.region_name or "eu-west-1"
    params["BoxLogGroup"] = args.box_log_group or params["BoxLogGroup"]
    params["AgentLogGroup"] = args.agent_log_group or params["AgentLogGroup"]
    for key, value in params.items():
        body = body.replace("${" + key + "}", value)
    widgets = json.loads(body)["widgets"]

    cw, logs = session.client("cloudwatch"), session.client("logs")
    end = datetime.now(timezone.utc)
    start = end - timedelta(hours=args.hours)
    failures = 0
    for widget in widgets:
        props = widget["properties"]
        if widget["type"] == "metric":
            queries = []
            for row in props["metrics"]:
                m = row[0]
                q = {"Id": m["id"], "Expression": m["expression"], "ReturnData": m.get("visible", True)}
                if m.get("label"):
                    q["Label"] = m["label"]
                queries.append(q)
            try:
                result = cw.get_metric_data(MetricDataQueries=queries, StartTime=start, EndTime=end)["MetricDataResults"]
                series = [f"{r['Label'] or r['Id']}={sum(r['Values']):g} ({len(r['Values'])} pts)" for r in result]
                errors = [m["Value"] for m in cw_messages(result)]
                print(f"{'OK  ' if not errors else 'FAIL'} metric  {props['title']}: {', '.join(series) or 'no series'}{' -- ' + '; '.join(errors) if errors else ''}")
                failures += bool(errors)
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL metric  {props['title']}: {exc}")
        elif widget["type"] == "log":
            sources = re.findall(r"SOURCE '([^']+)'", props["query"])
            query = re.sub(r"^(\s*SOURCE '[^']+'\s*\|\s*)+", "", props["query"])
            try:
                qid = logs.start_query(logGroupNames=sources, startTime=int(start.timestamp()), endTime=int(end.timestamp()), queryString=query)["queryId"]
                while (res := logs.get_query_results(queryId=qid))["status"] in ("Scheduled", "Running"):
                    time.sleep(1)
                rows = res["results"]
                print(f"{'OK  ' if res['status'] == 'Complete' else 'FAIL'} logs    {props['title']}: {res['status']}, {len(rows)} row(s)")
                for row in rows[:5]:
                    print("        " + ", ".join(f"{c['field']}={c['value']}" for c in row if not c["field"].startswith("@")))
                failures += res["status"] != "Complete"
            except Exception as exc:  # noqa: BLE001
                failures += 1
                print(f"FAIL logs    {props['title']}: {exc}")
    print(f"\n{'ALL WIDGETS OK' if not failures else f'{failures} WIDGET(S) FAILED'}")


def cw_messages(result: list[dict]) -> list[dict]:
    return [m for r in result for m in r.get("Messages", []) if m.get("Code") not in (None, "PartialData")]


if __name__ == "__main__":
    main()
