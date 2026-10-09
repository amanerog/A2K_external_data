"""Does the admission judge decide the same with less evidence? Runs the
basic judge (judge_basic.py) on recorded answers three times -- with the full
raw tool output, with it cut to the cited pages (evidence.cited_evidence_text)
and, outside the grounding domains, also with each tool call capped
(evidence.cap_sections) -- and compares what admission would do with each:
ACTIVE / STALE / NOT_FIT, the data date behind the expiry, and the cost.

The classifier runs once per answer; nothing is written anywhere but the
output files. Real Bedrock calls: roughly the cost of one admission per
answer and variant (a Linkup answer with full evidence ~0.10-0.30 USD).

Usage:
    python deploy/agentcore/measure_cited_evidence.py ../A2K_external_data/deploy/agentcore/routing_p1.jsonl \\
        [--per-vendor linkup=15,cala=5 | --ids 1,2,3] [--cap 20000] [--workers 4] --output cited_evidence.jsonl

Judge noise: Sonnet 5 takes no temperature, so the same evidence can get a
different verdict. --variants full --baseline cited_evidence.jsonl runs the
full evidence again on the same answers and compares it with the earlier
full run -- how much the judge disagrees with itself.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import boto3  # noqa: E402
from botocore.config import Config as BotoConfig  # noqa: E402

from a2k.cache import classifier, decision, judge_basic, usage  # noqa: E402
from a2k.cache.evidence import cap_sections, cited_evidence_text, evidence_text  # noqa: E402
from a2k.cache.expiry import default_policy, expires_at  # noqa: E402
from a2k.config import config  # noqa: E402

VARIANTS = ("full", "cited", "capped")


def load(path: str, ids: set[str] | None, per_vendor: dict[str, int]) -> list[dict]:
    rows = []
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        vendors = {tc.get("vendor") for tc in r.get("toolCalls") or [] if tc.get("vendor")}
        if not r.get("answer") or len(vendors) != 1 or r.get("coverage") in ("partial", "none"):
            continue  # never reaches the judge in production
        r["_vendor"] = vendors.pop()
        if ids is None or r["id"] in ids:
            rows.append(r)
    if ids is not None:
        return rows
    picked = []
    for vendor, n in per_vendor.items():
        mine = sorted((r for r in rows if r["_vendor"] == vendor), key=lambda r: int(r["id"]))
        step = max(1, len(mine) // n) if n else 1
        picked += mine[::step][:n]
    return picked


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("jsonl")
    parser.add_argument("--ids")
    parser.add_argument("--per-vendor", default="linkup=15,cala=5")
    parser.add_argument("--cap", type=int, default=20000)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", default="cited_evidence.jsonl")
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--baseline", help="earlier output: its full run is compared with this run's variants")
    args = parser.parse_args()
    variants = tuple(args.variants.split(","))
    baseline = {}
    if args.baseline:
        baseline = {r["id"]: r["variants"]["full"] for r in map(json.loads, open(args.baseline, encoding="utf-8")) if "full" in r.get("variants", {})}
        args.ids = args.ids or ",".join(baseline)

    per_vendor = {k: int(v) for k, v in (x.split("=") for x in args.per_vendor.split(","))}
    rows = load(args.jsonl, set(args.ids.split(",")) if args.ids else None, per_vendor)
    bedrock = boto3.client("bedrock-runtime", region_name=os.environ.get("AWS_REGION", "eu-west-1"),
                           config=BotoConfig(read_timeout=600, retries={"max_attempts": 4, "mode": "adaptive"}))
    policy, now = default_policy(), datetime.now(timezone.utc)
    print(f"{len(rows)} answer(s): {Counter(r['_vendor'] for r in rows)}; judge {config.admission_judge_model_id}", flush=True)

    def run(r: dict) -> dict:
        candidate = SimpleNamespace(tool_calls=r["toolCalls"], citations=r.get("citations") or [])
        out = {"id": r["id"], "vendor": r["_vendor"], "variants": {}}
        try:
            cls = classifier.classify(bedrock, model_id=config.admission_light_model_id, question=r["query"], today=now.date())
        except Exception as exc:  # noqa: BLE001
            out["error"] = f"classify: {type(exc).__name__}: {exc}"
            return out
        grounding = any(d in config.admission_grounding_domains for d in cls.domains)
        full = evidence_text(candidate)
        cited, how = cited_evidence_text(candidate)
        texts = {"full": full, "cited": cited, "capped": cited if grounding else cap_sections(cited, args.cap)}
        out.update(domain=cls.domain_primary, grounding=grounding, cut=how, self_contained=cls.self_contained)
        for name in variants:
            text = texts[name]
            v: dict = {"chars": len(text)}
            if len(text) > config.admission_max_evidence_chars:
                v["outcome"] = "FILTERED"
                out["variants"][name] = v
                continue
            with usage.track() as tracked:
                try:
                    f = judge_basic.judge(bedrock, model_id=config.admission_judge_model_id, question=r["query"], answer=r["answer"],
                                          evidence=text, classification=cls, citations=r.get("citations") or [], today=now.date(), grounding=grounding)
                except Exception as exc:  # noqa: BLE001
                    v.update(outcome="ERROR", reason=f"{type(exc).__name__}: {exc}")
                    out["variants"][name] = v
                    continue
            verdict = decision.decide_basic(f)
            outcome = "NOT_FIT"
            if verdict.fit:
                exp = expires_at(cls, f.data_as_of, now=now, policy=policy, data_kind=f.data_kind)
                outcome = "ACTIVE" if exp > now else "STALE"
            v.update(outcome=outcome, reason=verdict.reason, data_kind=f.data_kind, data_as_of=f.data_as_of.isoformat() if f.data_as_of else None,
                     elements=[e.status for e in f.elements], core={k: c["result"] for k, c in f.core.items()}, usage=tracked.summary())
            out["variants"][name] = v
        return out

    results, lock = [], Lock()
    with open(args.output, "w", encoding="utf-8") as fh, ThreadPoolExecutor(args.workers) as pool:
        for res in pool.map(run, rows):
            with lock:
                results.append(res)
                fh.write(json.dumps(res, ensure_ascii=False) + "\n")
                fh.flush()
                vs = res["variants"]
                print(f"id={res['id']:>4} {res['vendor']:<6} {res.get('domain', '?'):<4} "
                      + "  ".join(f"{n}={vs[n]['outcome']}/{vs[n].get('data_as_of')}/{vs[n]['chars'] // 1000}k" for n in variants if n in vs)
                      + (f"  ERROR {res['error']}" if res.get("error") else ""), flush=True)

    done = [r for r in results if not r.get("error")]
    print(f"\n{len(done)} answer(s) compared")
    if baseline:
        for r in done:
            r["variants"]["baseline full"] = baseline[r["id"]]
    reference = "baseline full" if baseline else "full"
    for name in [v for v in variants if v != reference or baseline]:
        if name == reference:
            continue
        same_outcome = sum(r["variants"][reference]["outcome"] == r["variants"][name]["outcome"] for r in done)
        same_date = sum(r["variants"][reference].get("data_as_of") == r["variants"][name].get("data_as_of") for r in done)
        same_kind = sum(r["variants"][reference].get("data_kind") == r["variants"][name].get("data_kind") for r in done)
        print(f"{name:>6} vs {reference}: same outcome {same_outcome}/{len(done)}, same data_as_of {same_date}/{len(done)}, same data_kind {same_kind}/{len(done)}")
        for r in done:
            a, b = r["variants"][reference], r["variants"][name]
            if a["outcome"] != b["outcome"]:
                print(f"         id={r['id']} {r['vendor']} {r.get('domain')}: full {a['outcome']} ({a.get('reason', '')[:70]}) -> {name} {b['outcome']} ({b.get('reason', '')[:70]})")
    for vendor in sorted({r["vendor"] for r in done}):
        mine = [r for r in done if r["vendor"] == vendor]
        costs = {n: [r["variants"][n]["usage"]["cost_usd"] for r in mine if "usage" in r["variants"][n]] for n in variants}
        print(f"{vendor}: mean judge cost " + ", ".join(f"{n} {statistics.mean(c):.3f}" for n, c in costs.items() if c) + " USD")
    print("by outcome: " + "; ".join(f"{n} {dict(Counter(r['variants'][n]['outcome'] for r in done))}" for n in variants))


if __name__ == "__main__":
    main()
