"""Vendor self-consistency: the baseline for the go / no-go false-hit gate.

The shadow check (a2k/cache/shadow_check.py) compares a cached answer with a
fresh vendor answer to the same question; "contradicts" or "cached_outdated"
counts as a false hit. But the fresh answer is not the truth -- in CRP, 33 of
34 checks of 2026-10-22 were the very same question (cosine 1.0), and most
contradictions were the vendor giving a different address or relation from
one call to the next. This measures that noise floor: each question goes to
the agent twice in a row -- straight to its Runtime, so no cache lookup, no
admission, nothing in the dashboard -- and the two answers are compared by
the same Checker with the same prompt. The cache's false-hit rate is only
meaningful next to this one.

A is the first answer and plays the cached one, B the second plays the fresh
one; both were fetched minutes apart, so "cached_outdated" here is noise too.

Questions come from a ground-truth CSV (the input one, ID/Client quesiton;
or a run_ground_truth_eval.py results one, id/query), optionally only those
the box classified into --domain within the last --hours (cache.retrieval
lines, joined on query_sha256).

Cost: two agent runs per question -- about 1.2 USD of agent plus the vendor
calls (a Cala answer makes ~4 calls at 0.62 EUR), so 5-7 EUR per Cala
question. 30 questions are enough to compare with the cache's 10/34.

Usage (credentials for the account in the environment; boto3 is the only
dependency, Python 3.9+):
    python3 deploy/agentcore/vendor_self_consistency.py \\
        --input ../A2K_external_data/deploy/agentcore/gt_pocv3_cache_20261022.csv \\
        --domain CRP --hours 24 --limit 30 --output self_consistency_crp.csv
"""

from __future__ import annotations

import argparse
import base64
import csv
import json
import math
import os
import sys
import threading
import time
import types
import urllib.error
import urllib.parse
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Run from anywhere: put the repo root (two levels up from deploy/agentcore/)
# on sys.path so `import a2k...` works without installing the package first.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
try:
    import dotenv  # noqa: F401 -- a2k.config loads a .env with it
except ImportError:  # only boto3 is needed here: a2k.config's .env loading is a no-op without it
    sys.modules["dotenv"] = types.SimpleNamespace(load_dotenv=lambda *a, **k: None)

from a2k.cache import shadow_check  # noqa: E402
from a2k.logline import query_hash  # noqa: E402

BOX_RUNTIME_ID = "a2k_cache_mcp-48ImkT2F9a"
BOX_LOG_GROUP = "/aws/bedrock-agentcore/runtimes/a2k_cache_mcp-48ImkT2F9a-DEFAULT"
DEFAULT_CHECKER_MODEL = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"  # a2k/config.py cache_checker_model_id
QUESTION_COLUMNS = ("query", "client quesiton", "client question", "question")
ID_COLUMNS = ("id",)

DOMAINS_QUERY = (
    'filter event = "cache.retrieval" and ispresent(query_sha256) '
    "| fields @timestamp, query_sha256, domains.0 as domain | sort @timestamp asc | limit 10000"
)


# --- questions -------------------------------------------------------------


def load_questions(path: Path) -> list[dict[str, str]]:
    """[{"id", "query"}] from a ground-truth input or results CSV, whatever
    its delimiter and the spelling of its question column."""
    text = path.read_text(encoding="utf-8-sig")
    delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
    reader = csv.DictReader(text.splitlines(keepends=True), delimiter=delimiter)
    columns = {(name or "").strip().lower(): name for name in reader.fieldnames or []}
    q_col = next((columns[c] for c in QUESTION_COLUMNS if c in columns), None)
    if q_col is None:
        sys.exit(f"{path}: no question column (tried {QUESTION_COLUMNS}) in {reader.fieldnames}")
    id_col = next((columns[c] for c in ID_COLUMNS if c in columns), None)
    out = []
    for n, row in enumerate(reader, 1):
        query = (row.get(q_col) or "").strip()
        if query:
            out.append({"id": (row.get(id_col) or "").strip() if id_col else str(n), "query": query})
    return out


def select(questions: list[dict[str, str]], *, ids: Optional[set[str]], domain_of: Optional[dict[str, str]], domain: Optional[str], limit: Optional[int]) -> list[dict[str, str]]:
    picked = [q for q in questions if ids is None or q["id"] in ids]
    if domain:
        picked = [q for q in picked if (domain_of or {}).get(query_hash(q["query"])) == domain]
    return picked[:limit] if limit else picked


def domains_by_hash(rows: list[dict[str, str]]) -> dict[str, str]:
    """query_sha256 -> the domain the box classified it into (latest line wins)."""
    return {r["query_sha256"]: r.get("domain") or "NONE" for r in rows if r.get("query_sha256")}


# --- the summary -----------------------------------------------------------


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95 % Wilson interval of k/n, in %: with a few dozen questions the
    interval says more than the rate."""
    if n == 0:
        return 0.0, 0.0
    p = k / n
    centre = (p + z * z / (2 * n)) / (1 + z * z / n)
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / (1 + z * z / n)
    return round(100 * max(0.0, centre - half), 1), round(100 * min(1.0, centre + half), 1)


def summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    counts: dict[str, int] = {}
    for r in results:
        counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1
    compared = sum(n for v, n in counts.items() if v in shadow_check.VERDICTS)
    false_hits = sum(counts.get(v, 0) for v in shadow_check.FALSE_HIT)
    return {
        "counts": counts,
        "compared": compared,
        "false_hits": false_hits,
        "false_hit_pct": round(100 * false_hits / compared, 1) if compared else None,
        "ci95": wilson(false_hits, compared),
    }


# --- AWS -------------------------------------------------------------------


class Agent:
    """The agent Runtime, called the way a2k-box calls it (engine._call_agent):
    a Cognito client-credentials token and a plain POST to its URL. Its
    settings come from the box Runtime's own environment and secret."""

    def __init__(self, session, box_runtime_id: str):
        env = session.client("bedrock-agentcore-control").get_agent_runtime(agentRuntimeId=box_runtime_id).get("environmentVariables") or {}
        secret = {}
        if env.get("A2K_SECRETS_ARN"):
            secret = json.loads(session.client("secretsmanager").get_secret_value(SecretId=env["A2K_SECRETS_ARN"])["SecretString"])
        setting = lambda name: env.get(name) or secret.get(name)  # noqa: E731
        self.url = setting("AGENT_RUNTIME_URL")
        self.token_url = setting("AGENT_POOL_TOKEN_URL")
        self.scope = setting("AGENT_POOL_SCOPE")
        self.client_id = setting("AGENT_POOL_CLIENT_ID")
        self.client_secret = setting("AGENT_POOL_CLIENT_SECRET")
        self.checker_model = env.get("CACHE_CHECKER_MODEL_ID") or DEFAULT_CHECKER_MODEL
        self.cards_table = env.get("VENDOR_CARDS_TABLE")
        missing = [n for n, v in (("AGENT_RUNTIME_URL", self.url), ("AGENT_POOL_TOKEN_URL", self.token_url), ("AGENT_POOL_CLIENT_ID", self.client_id), ("AGENT_POOL_CLIENT_SECRET", self.client_secret)) if not v]
        if missing:
            sys.exit(f"the box Runtime {box_runtime_id} has no {missing} (environment or A2K_SECRETS_ARN)")
        self.catalogue = vendor_catalogue(session, self.cards_table)
        self._token: Optional[str] = None
        self._token_expires = 0.0
        self._lock = threading.Lock()

    def token(self, *, renew: bool = False) -> str:
        with self._lock:
            if renew or not self._token or time.time() >= self._token_expires:
                data = {"grant_type": "client_credentials", **({"scope": self.scope} if self.scope else {})}
                basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode()).decode()
                req = urllib.request.Request(self.token_url, data=urllib.parse.urlencode(data).encode(), headers={"Authorization": f"Basic {basic}", "Content-Type": "application/x-www-form-urlencoded"})
                with urllib.request.urlopen(req, timeout=30) as resp:
                    payload = json.load(resp)
                self._token = payload["access_token"]
                self._token_expires = time.time() + float(payload.get("expires_in", 3600)) - 30
            return self._token

    def ask(self, query: str) -> dict[str, Any]:
        """The agent's content for one ask (answer, toolCalls, ...). Raises on failure."""
        body = json.dumps({"operation": "ask", "query": query, "sources": None, "catalogue": self.catalogue, "requestId": f"self-consistency-{uuid.uuid4()}"}).encode()
        for attempt in (1, 2):
            req = urllib.request.Request(self.url, data=body, headers={"Authorization": f"Bearer {self.token(renew=attempt == 2)}", "Content-Type": "application/json", "Accept": "application/json"})
            try:
                with urllib.request.urlopen(req, timeout=600) as resp:  # 600 s: engine._call_agent's own ceiling
                    out = json.load(resp)
                break
            except urllib.error.HTTPError as exc:
                if exc.code == 401 and attempt == 1:
                    continue
                raise
        if not out.get("ok"):
            raise RuntimeError(f"agent reported failure: {out.get('error')}")
        return out.get("content") or {}


def vendor_catalogue(session, table_name: Optional[str]) -> list[dict[str, Any]]:
    """The catalogue a2k-box passes to the agent (a2k/cards vendor_catalogue()),
    read from the same DynamoDB table -- the agent routes with it."""
    if not table_name:
        sys.exit("the box Runtime has no VENDOR_CARDS_TABLE")
    table = session.resource("dynamodb").Table(table_name)
    items, kwargs = [], {}
    while True:
        page = table.scan(**kwargs)
        items += page.get("Items", [])
        if not page.get("LastEvaluatedKey"):
            break
        kwargs["ExclusiveStartKey"] = page["LastEvaluatedKey"]
    catalogue = []
    for item in sorted(items, key=lambda i: i["sourceId"]):
        card = json.loads(item["cardJson"])
        profile = card.get("knowledgeProfile") or {}
        catalogue.append({
            "sourceId": item["sourceId"],
            "name": card.get("name"),
            "domains": profile.get("domains"),
            "topics": profile.get("topics"),
            "scope": (profile.get("coverage") or {}).get("scope"),
            "status": ((card.get("enterprise") or {}).get("lifecycle") or {}).get("status"),
            "priority": card.get("priority"),
            "queryType": card.get("queryType"),
        })
    return catalogue


def query_domains(logs, log_group: str, hours: float) -> dict[str, str]:
    end = datetime.now(timezone.utc)
    qid = logs.start_query(logGroupNames=[log_group], startTime=int((end - timedelta(hours=hours)).timestamp()), endTime=int(end.timestamp()), queryString=DOMAINS_QUERY)["queryId"]
    while (res := logs.get_query_results(queryId=qid))["status"] in ("Scheduled", "Running"):
        time.sleep(1)
    if res["status"] != "Complete":
        sys.exit(f"Logs Insights query {res['status']}")
    return domains_by_hash([{c["field"]: c["value"] for c in row} for row in res["results"]])


# --- one question ----------------------------------------------------------


def _vendors(content: dict[str, Any]) -> str:
    return ",".join(sorted({tc["vendor"] for tc in content.get("toolCalls") or [] if tc.get("vendor")})) or "none"


def run_one(agent: Agent, bedrock, q: dict[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {"id": q["id"], "query": q["query"], "verdict": "ERROR", "reason": "", "vendors_a": "", "vendors_b": "", "ms_a": "", "ms_b": "", "answer_a": "", "answer_b": ""}
    try:
        answers = []
        for side in ("a", "b"):
            t0 = time.monotonic()
            content = agent.ask(q["query"])
            out[f"ms_{side}"] = int((time.monotonic() - t0) * 1000)
            out[f"vendors_{side}"] = _vendors(content)
            out[f"answer_{side}"] = content.get("answer") or ""
            answers.append(out[f"answer_{side}"])
        if not all(answers):
            out["reason"] = "an answer came back empty"
            return out
        today = datetime.now(timezone.utc).date()
        out["verdict"], out["reason"] = shadow_check.compare(bedrock, model_id=agent.checker_model, question=q["query"], cached_answer=answers[0], fresh_answer=answers[1], today=today, cached_on=today)
    except Exception as exc:  # noqa: BLE001 -- one question must not stop the run
        out["reason"] = f"{type(exc).__name__}: {exc}"[:500]
    return out


FIELDS = ["id", "verdict", "reason", "vendors_a", "vendors_b", "ms_a", "ms_b", "query", "answer_a", "answer_b"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", required=True, type=Path, help="ground-truth input or results CSV")
    parser.add_argument("--output", default="self_consistency.csv", type=Path)
    parser.add_argument("--ids", default=None, help="comma-separated ids")
    parser.add_argument("--domain", default=None, help="only questions the box classified into this domain (e.g. CRP)")
    parser.add_argument("--hours", type=float, default=24, help="window of cache.retrieval lines for --domain")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=2, help="questions in parallel (each asks A then B)")
    parser.add_argument("--box-runtime-id", default=BOX_RUNTIME_ID)
    parser.add_argument("--box-log-group", default=BOX_LOG_GROUP)
    parser.add_argument("--region", default=None)
    args = parser.parse_args()

    import boto3

    session = boto3.session.Session(region_name=args.region or os.environ.get("AWS_REGION") or "eu-west-1")
    questions = load_questions(args.input)
    domain_of = query_domains(session.client("logs"), args.box_log_group, args.hours) if args.domain else None
    picked = select(questions, ids=set(args.ids.split(",")) if args.ids else None, domain_of=domain_of, domain=args.domain, limit=args.limit)
    if not picked:
        sys.exit(f"no question selected out of {len(questions)} (check --domain/--hours/--ids)")
    agent = Agent(session, args.box_runtime_id)
    bedrock = session.client("bedrock-runtime")
    runtime = urllib.parse.unquote(agent.url.split("/invocations")[0].rsplit("/", 1)[-1]).rsplit("/", 1)[-1]
    print(f"{len(picked)} question(s), each asked twice to {runtime} -- checker {agent.checker_model}; writing {args.output}")

    lock, results = threading.Lock(), []
    with args.output.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDS)
        writer.writeheader()

        def work(q: dict[str, str]) -> None:
            r = run_one(agent, bedrock, q)
            with lock:
                results.append(r)
                writer.writerow({k: r[k] for k in FIELDS})
                f.flush()
                print(f"id={r['id']} {r['verdict']} ({r['vendors_a']} / {r['vendors_b']}) {r['reason'][:140]}", flush=True)

        with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
            list(pool.map(work, picked))

    s = summary(results)
    print(f"\n=== Vendor self-consistency ({args.domain or 'all domains'}) ===")
    for verdict, n in sorted(s["counts"].items(), key=lambda kv: -kv[1]):
        print(f"{verdict:22s} {n:3d}")
    if s["compared"]:
        print(f"\nfalse-hit-like (contradicts + cached_outdated): {s['false_hits']}/{s['compared']} = {s['false_hit_pct']} % (95 % CI {s['ci95'][0]}-{s['ci95'][1]} %)")
        print("Compare with the cache's false_hit_pct in the go / no-go table for the same domain.")


if __name__ == "__main__":
    main()
