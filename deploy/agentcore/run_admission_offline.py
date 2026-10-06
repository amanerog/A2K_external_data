"""Runs the cache admission pipeline (a2k/cache/admission.py, steps 2-8)
over answers already recorded by run_ground_truth_eval.py -- no Aurora, no
S3, nothing written anywhere but the output files. Real Bedrock calls
(classifier, judge, translation), so it costs what a real admission costs.

Compares each admission outcome with the POC grading of the same answer
(the .csv next to each .jsonl): FIT should line up with ACCEPTABLE + Cubierta
+ no hallucination. Bear in mind they measure different things -- the POC
judge compares against a reference answer, the admission judge against the
raw tool output -- so disagreements are what to read, not just count.

T5 (sources real) is skipped: the POCv3 .jsonl files do not keep the agent's
citations (if a record has a "citations" list, the judge still gets it).
Embedding is skipped too (nothing reads the vector offline).

Usage:
    python run_admission_offline.py ground_truth_pocv3_cala_v6_results.jsonl \\
        [ground_truth_pocv3_linkup_results.jsonl ...] [--ids 1,2,3] [--workers 4] \\
        --output admission_offline.csv

Writes <output>.csv (one row per answer) and <output>.jsonl (full findings).
Needs AWS credentials with bedrock:Converse on the light and judge models.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import statistics
import sys
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import boto3  # noqa: E402
from botocore.config import Config as BotoConfig  # noqa: E402

from a2k.cache import admission  # noqa: E402
from a2k.cache.expiry import default_policy  # noqa: E402
from a2k.cache.models import Candidate  # noqa: E402
from a2k.config import config  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-west-1")


def load_rows(jsonl_paths: list[str], ids: set[str] | None) -> list[tuple[dict, dict]]:
    rows = []
    for path in jsonl_paths:
        csv_path = Path(path).with_suffix(".csv")
        grades = {r["id"]: r for r in csv.DictReader(open(csv_path, encoding="utf-8"))} if csv_path.exists() else {}
        for line in open(path, encoding="utf-8"):
            if not line.strip():
                continue
            record = json.loads(line)
            if ids and record["id"] not in ids:
                continue
            if not record.get("ok") or record.get("error") or not record.get("answer"):
                continue
            rows.append((record, grades.get(record["id"], {})))
    return rows


def candidate_from(record: dict) -> Candidate:
    vendors = {tc.get("vendor") for tc in record.get("toolCalls") or [] if tc.get("vendor")}
    return Candidate(
        entry_id=f"{record.get('vendor')}-{record['id']}",
        question=record["query"],
        answer=record["answer"],
        vendor=record.get("vendor") or next(iter(vendors), ""),
        tool_calls=record.get("toolCalls") or [],
        # Only present in .jsonl files written after run_ground_truth_eval.py
        # started recording citations; the judge sees them as the answer's sources.
        citations=record.get("citations") or [],
        created_at=datetime.now(timezone.utc),
    )


def poc_good(grade: dict) -> bool | None:
    if not grade:
        return None
    return grade.get("verdict") == "ACCEPTABLE" and grade.get("final_verdict") == "Cubierta" and str(grade.get("hallucination")).lower() != "true"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("jsonl", nargs="+")
    parser.add_argument("--ids", default=None, help="comma-separated ids to run (default: all)")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--output", default="admission_offline.csv")
    parser.add_argument("--disabled-domains", default="", help="comma-separated domain codes switched off, e.g. MED")
    parser.add_argument("--no-confirm", action="store_true", help="strict mode: single judgement, no confirming second call")
    parser.add_argument("--mode", choices=("basic", "strict"), default=config.admission_mode, help="admission mode (default: ADMISSION_MODE, basic)")
    parser.add_argument("--grounding-domains", default=",".join(config.admission_grounding_domains), help="basic mode: domains checked for grounding (C3)")
    args = parser.parse_args()

    ids = set(args.ids.split(",")) if args.ids else None
    rows = load_rows(args.jsonl, ids)
    settings = admission.AdmissionSettings(
        light_model_id=config.admission_light_model_id,
        judge_model_id=config.admission_judge_model_id,
        embedding_model_id=config.cache_embedding_model_id,
        min_answer_chars=config.admission_min_answer_chars,
        max_evidence_chars=config.admission_max_evidence_chars,
        require_verified_quotes=config.admission_require_verified_quotes,
        check_sources=False,
        disabled_domains=tuple(d.strip().upper() for d in args.disabled_domains.split(",") if d.strip()),
        confirm_fit=not args.no_confirm,
        mode=args.mode,
        grounding_domains=tuple(d.strip().upper() for d in args.grounding_domains.split(",") if d.strip()),
    )
    bedrock = boto3.client("bedrock-runtime", region_name=REGION, config=BotoConfig(read_timeout=300, retries={"max_attempts": 4, "mode": "adaptive"}))
    policy = default_policy()
    print(f"{len(rows)} answer(s), mode={settings.mode}, judge={settings.judge_model_id}, light={settings.light_model_id}", flush=True)

    out_csv = Path(args.output)
    out_jsonl = out_csv.with_suffix(".jsonl")
    fields = [
        "vendor", "id", "outcome", "reason", "poc_good", "poc_verdict", "poc_containment",
        "domain_primary", "domain_secondary", "self_contained", "question_type", "depth", "n_elements", "elements_covered", "failed_checks",
        "data_kind", "data_as_of", "expires_at", "ms_classify", "ms_judge", "ms_judge_confirm", "ms_normalize", "ms_total", "query",
    ]
    lock = Lock()
    results = []

    def run(record: dict, grade: dict) -> dict:
        candidate = candidate_from(record)
        t0 = datetime.now(timezone.utc)
        try:
            ev = admission.evaluate(candidate, bedrock_client=bedrock, settings=settings, expiry_policy=policy, embed_enabled=False)
            result = ev.result
        except Exception as exc:  # noqa: BLE001 -- one bad row must not stop the batch
            result = admission.AdmissionResult(candidate.entry_id, "ERROR", f"{type(exc).__name__}: {exc}")
        total_ms = int((datetime.now(timezone.utc) - t0).total_seconds() * 1000)
        c, f = result.classification, result.findings
        failed = []
        if f:
            failed = [k for k, v in {**f.core, **f.modules}.items() if v["result"] == "fail"]
        return {
            "vendor": candidate.vendor,
            "id": record["id"],
            "outcome": result.outcome,
            "reason": result.reason,
            "poc_good": poc_good(grade),
            "poc_verdict": grade.get("verdict"),
            "poc_containment": grade.get("final_verdict"),
            "domain_primary": c.domain_primary if c else None,
            "domain_secondary": c.domain_secondary if c else None,
            "self_contained": c.self_contained if c else None,
            "question_type": c.question_type if c else None,
            "depth": c.depth if c else None,
            "n_elements": len(c.requested_elements) if c else None,
            "elements_covered": sum(e.status == "covered" for e in f.elements) if f else None,
            "failed_checks": ",".join(failed),
            "data_kind": f.data_kind if f else None,
            "data_as_of": f.data_as_of.isoformat() if f and f.data_as_of else None,
            "expires_at": result.expires_at.isoformat() if result.expires_at else None,
            "ms_classify": result.timings_ms.get("classify"),
            "ms_judge": result.timings_ms.get("judge"),
            "ms_judge_confirm": result.timings_ms.get("judge_confirm"),
            "ms_normalize": result.timings_ms.get("normalize"),
            "ms_total": total_ms,
            "query": record["query"][:200],
            "_full": {
                "classification": asdict(c) if c else None,
                "findings": admission._findings_json(result),
                "timings_ms": result.timings_ms,
            },
        }

    with open(out_csv, "w", newline="", encoding="utf-8") as fcsv, open(out_jsonl, "w", encoding="utf-8") as fj:
        writer = csv.DictWriter(fcsv, fieldnames=fields)
        writer.writeheader()
        with ThreadPoolExecutor(max_workers=args.workers) as pool:
            futures = [pool.submit(run, record, grade) for record, grade in rows]
            for i, future in enumerate(as_completed(futures), start=1):
                row = future.result()
                with lock:
                    results.append(row)
                    full = row.pop("_full")
                    writer.writerow(row)
                    fcsv.flush()
                    fj.write(json.dumps({**row, **full}, ensure_ascii=False, default=str) + "\n")
                    fj.flush()
                print(f"[{i}/{len(rows)}] {row['vendor']} id={row['id']} -> {row['outcome']} ({row['ms_total']} ms) {row['reason'][:90]}", flush=True)

    print("\n=== Outcomes by vendor ===")
    by_vendor = defaultdict(Counter)
    for r in results:
        by_vendor[r["vendor"]][r["outcome"]] += 1
    for vendor, counts in sorted(by_vendor.items()):
        print(f"{vendor:8s} {dict(counts)}")

    graded = [r for r in results if r["poc_good"] is not None and r["outcome"] in ("ACTIVE", "NOT_FIT", "FILTERED", "DOMAIN_OFF", "STALE")]
    admitted = [r for r in graded if r["outcome"] == "ACTIVE"]
    good = [r for r in graded if r["poc_good"]]
    if graded:
        tp = sum(1 for r in admitted if r["poc_good"])
        print("\n=== Against POC grading (ACCEPTABLE + Cubierta + no hallucination) ===")
        print(f"admitted {len(admitted)}/{len(graded)} | POC-good {len(good)} | both {tp}")
        if admitted:
            print(f"precision vs POC: {tp / len(admitted):.0%}  (admitted that POC also rated good)")
        if good:
            print(f"recall vs POC:    {tp / len(good):.0%}  (POC-good that were admitted)")

    print("\n=== Latency (s) ===")
    for step in ("ms_classify", "ms_judge", "ms_judge_confirm", "ms_normalize", "ms_total"):
        values = sorted(r[step] / 1000 for r in results if r[step])
        if values:
            print(f"{step[3:]:10s} p50 {statistics.median(values):6.1f}  p90 {values[int(0.9 * (len(values) - 1))]:6.1f}  max {values[-1]:6.1f}")
    print(f"\nWrote {out_csv} and {out_jsonl}")


if __name__ == "__main__":
    main()
