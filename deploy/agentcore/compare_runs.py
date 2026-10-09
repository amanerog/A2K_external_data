"""Compares two run_ground_truth_eval.py runs on the questions they share:
time, answer length, the agent's tokens and estimated cost, and the ground
truth grading (verdict, containment) -- overall, by vendor, and question by
question where the verdict changed.

Usage:
    python deploy/agentcore/compare_runs.py BASELINE.jsonl NEW.jsonl
(each .jsonl with its graded .csv next to it)
"""

from __future__ import annotations

import argparse
import csv
import json
import statistics as st
from pathlib import Path

# Sonnet 4.5 list prices, USD per million tokens (the agent's model).
PRICE_IN, PRICE_OUT = 3.0, 15.0


def load(path: str) -> dict[str, dict]:
    rows = {r["id"]: r for r in map(json.loads, open(path, encoding="utf-8")) if r.get("id")}
    grades = {r["id"]: r for r in csv.DictReader(open(Path(path).with_suffix(".csv"), encoding="utf-8-sig"))}
    for i, r in rows.items():
        g = grades.get(i, {})
        r["_verdict"], r["_containment"] = g.get("verdict"), g.get("final_verdict")
        r["_vendor"] = ",".join(sorted({t["vendor"] for t in r.get("toolCalls") or [] if t.get("vendor")})) or "none"
    return rows


def stats(rows: list[dict]) -> dict:
    def med(key):
        vals = [key(r) for r in rows if key(r) is not None]
        return st.median(vals) if vals else None
    cost = [(r["inputTokens"] * PRICE_IN + r["outputTokens"] * PRICE_OUT) / 1e6 for r in rows if r.get("inputTokens") and r.get("outputTokens")]
    return {
        "n": len(rows),
        "time_s": med(lambda r: r.get("latencyMs") and r["latencyMs"] / 1000),
        "answer_chars": med(lambda r: len(r.get("answer") or "")),
        "output_tok": med(lambda r: r.get("outputTokens")),
        "input_tok": med(lambda r: r.get("inputTokens")),
        "agent_usd": st.mean(cost) if cost else None,
        "acceptable": sum(r["_verdict"] == "ACCEPTABLE" for r in rows),
        "covered": sum(r["_containment"] == "Cubierta" for r in rows),
        "partial": sum(r["_containment"] == "Parcial" for r in rows),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("baseline")
    parser.add_argument("new")
    args = parser.parse_args()
    base, new = load(args.baseline), load(args.new)
    ids = sorted(set(base) & set(new), key=int)
    print(f"{len(ids)} questions in both runs\n")
    fmt = lambda v: "-" if v is None else (f"{v:.3f}" if isinstance(v, float) and v < 1 else f"{v:,.0f}" if isinstance(v, (int, float)) else str(v))
    groups = {"all": ids}
    for v in sorted({new[i]["_vendor"] for i in ids}):
        groups[f"vendor {v} (new run)"] = [i for i in ids if new[i]["_vendor"] == v]
    for name, sel in groups.items():
        a, b = stats([base[i] for i in sel]), stats([new[i] for i in sel])
        print(f"== {name} ({len(sel)})")
        for k in ("time_s", "answer_chars", "output_tok", "input_tok", "agent_usd", "acceptable", "covered", "partial"):
            print(f"   {k:13} {fmt(a[k]):>10} -> {fmt(b[k]):>10}")
    print("\nverdict changes:")
    for i in ids:
        if base[i]["_verdict"] != new[i]["_verdict"] or base[i]["_containment"] != new[i]["_containment"]:
            print(f"   id={i:>4} {base[i]['_vendor']:>6} -> {new[i]['_vendor']:<6} {base[i]['_verdict']}/{base[i]['_containment']} -> "
                  f"{new[i]['_verdict']}/{new[i]['_containment']}  ({len(base[i].get('answer') or '')} -> {len(new[i].get('answer') or '')} chars)")


if __name__ == "__main__":
    main()
