"""Re-runs ONLY the judge (no live a2k.ask call) over rows in an existing
run_ground_truth_eval.py output CSV whose grading itself failed -- confirmed live
2026-09-25 on ground_truth_v3_linkup_results.csv: 16/120 rows had a real
`actual_answer` captured (the agent call succeeded) but the judge's own JSON
response got cut off mid-string (JSONDecodeError), leaving verdict=ERROR and no
usable grade even though nothing about the underlying answer was actually wrong.

Only re-grades rows where there's an `actual_answer` to grade in the first place --
rows whose `error` column starts with `{"code": "UPSTREAM_ERROR"` (the a2k.ask call
itself failed, envelope ok=false, no answer was ever produced) are left untouched
and reported separately: those need the live call retried
(run_ground_truth_eval.py --ids ..., not this script), re-grading can't manufacture
an answer that was never captured.

Only the "final" containment pass is re-run, not "raw" -- run_ground_truth_eval.py's
own output CSV never persisted `envelope["toolCalls"]` (the raw vendor tool output
judge_consolidation's raw-output pass needs), so that data is simply gone for any
row graded before this script existed. raw_verdict/raw_justification/
consolidation_loss are left as whatever the original run produced (or blank, if the
original run never reached that pass either) -- not recomputed, not guessed at.

Usage:
    python regrade_ground_truth_results.py ground_truth_v3_linkup_results.csv
    python regrade_ground_truth_results.py results.csv --output results_regraded.csv
    python regrade_ground_truth_results.py results.csv --force   # also re-grade
        # rows that already have a valid verdict, not just the errored ones
"""

from __future__ import annotations

import argparse
import csv
import sys
from pathlib import Path

import boto3

from judge import (
    DEFAULT_JUDGE_MODEL_ID,
    JudgeResult,
    judge as _judge_call,
    judge_containment,
)

REGION = "eu-west-1"
_UPSTREAM_ERROR_PREFIX = '{"code": "UPSTREAM_ERROR"'


def _needs_regrade(row: dict, force: bool) -> bool:
    if not row.get("actual_answer"):
        return False  # nothing to grade -- an upstream/call-level failure, not ours to fix
    if row.get("error", "").startswith(_UPSTREAM_ERROR_PREFIX):
        return False  # belt-and-suspenders: same check, in case actual_answer got set anyway
    if force:
        return True
    return row.get("verdict") == "ERROR" or not row.get("final_verdict")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("input", help="Existing run_ground_truth_eval.py output CSV to regrade")
    parser.add_argument("--output", default=None, help="Where to write the regraded CSV (default: overwrite input)")
    parser.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL_ID, help="Bedrock model id used to grade answers")
    parser.add_argument("--force", action="store_true", help="Regrade every row with an actual_answer, not just errored ones")
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"error: {input_path} not found", file=sys.stderr)
        sys.exit(1)
    output_path = Path(args.output) if args.output else input_path

    with input_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = reader.fieldnames
        rows = list(reader)

    to_regrade = [r for r in rows if _needs_regrade(r, args.force)]
    skipped_upstream = sum(1 for r in rows if r.get("error", "").startswith(_UPSTREAM_ERROR_PREFIX))
    print(f"{len(rows)} row(s) total -- {len(to_regrade)} to regrade, {skipped_upstream} skipped (upstream call failure, no answer to grade)")

    bedrock_client = boto3.client("bedrock-runtime", region_name=REGION)

    fixed = 0
    for i, row in enumerate(to_regrade, 1):
        print(f"[{i}/{len(to_regrade)}] id={row['id']} {row['query'][:70]!r}", flush=True)
        try:
            grade: JudgeResult = _judge_call(bedrock_client, args.judge_model, row["query"], row["source"], row["expected_answer"], row["actual_answer"])
            containment = judge_containment(
                bedrock_client,
                args.judge_model,
                row["query"],
                row["expected_answer"],
                row["actual_answer"],
                pass_label="final, already-consolidated answer the agent produced",
            )
            row["factual_accuracy"] = grade.factual_accuracy
            row["completeness"] = grade.completeness
            row["traceability"] = grade.traceability
            row["relevance_clarity"] = grade.relevance_clarity
            row["weighted_score"] = grade.weighted_score
            row["critical_omission"] = grade.critical_omission
            row["hallucination"] = grade.hallucination
            row["missing_facts"] = grade.missing_facts
            row["incorrect_facts"] = grade.incorrect_facts
            row["invented_facts"] = grade.invented_facts
            row["verdict"] = grade.verdict
            row["justification"] = grade.justification
            row["final_verdict"] = containment.verdict
            row["final_justification"] = containment.justification
            row["error"] = ""
            fixed += 1
            print(f"    -> {grade.verdict} / final={containment.verdict}", flush=True)
        except Exception as exc:  # noqa: BLE001 -- one bad row must not kill the whole batch
            row["error"] = f"{type(exc).__name__}: {exc}"
            print(f"    -> still failing: {row['error']}", flush=True)

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"\n{fixed}/{len(to_regrade)} row(s) regraded successfully -- wrote {output_path}")
    if skipped_upstream:
        print(f"{skipped_upstream} row(s) still need a live retry (upstream call failure) -- see run_ground_truth_eval.py --ids")


if __name__ == "__main__":
    main()
