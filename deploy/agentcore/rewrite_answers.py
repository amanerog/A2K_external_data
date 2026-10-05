"""Experiment: would the cache admit more answers if the agent wrote them
differently from the same vendor data?

Re-writes each recorded answer from its own recorded tool output -- no
vendor call, no tool loop -- with the agent's real system prompt
(agent/direct_agent.py, operation "ask"), in one of two arms:

  control  the prompt as it is today
  rules    the same prompt + WRITING_RULES below, aimed at the failures the
           attribution of the admission rejections found (attribute_rejections.py,
           2026-10-05): unsupported statements, dates dropped, sources not named,
           identifiers left out

Both arms re-write, so the comparison isolates the rules from the effect of
re-writing itself (one shot over a fixed tool output, instead of the agent
loop). The output is a .jsonl in the ground-truth format, with the recorded
POC grades copied next to it (they grade the ORIGINAL answer -- a reference
only), ready for run_admission_offline.py.

Usage:
    python rewrite_answers.py $GT/ground_truth_pocv3_cala_v6_results.jsonl --arm control \\
        --output rewrite_control.jsonl
    python rewrite_answers.py $GT/ground_truth_pocv3_cala_v6_results.jsonl --arm rules \\
        --output rewrite_rules.jsonl
    python run_admission_offline.py rewrite_control.jsonl --no-confirm --output admission_rewrite_control.csv
    python run_admission_offline.py rewrite_rules.jsonl --no-confirm --output admission_rewrite_rules.csv
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import Lock

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for path in (_REPO_ROOT, os.path.join(_REPO_ROOT, "agent")):
    if path not in sys.path:
        sys.path.insert(0, path)

import boto3  # noqa: E402
from botocore.config import Config as BotoConfig  # noqa: E402

import direct_agent  # noqa: E402
from a2k.cache.bedrock import converse_json  # noqa: E402
from a2k.cache.evidence import evidence_text  # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
from run_admission_offline import candidate_from, load_rows  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-west-1")
DEFAULT_MODEL = os.environ.get("BEDROCK_MODEL_ID", "eu.anthropic.claude-sonnet-4-5-20250929-v1:0")

WRITING_RULES = """
## WRITE AN ANSWER EVERY STATEMENT OF WHICH CAN BE CHECKED

This answer may be stored and served again to other users, after an automatic check of every
statement in it against the output of the tools you called. A statement that cannot be found
there, at the strength you state it, gets the whole answer rejected. So:

1. State only what the tool output says, at the strength it says it. A scheduled event has not
happened yet; a draft or a consultation is not a rule in force; the closest match a search
returned is not the entity asked about (say that it is the closest match and that no exact match
was found); a list of articles is not a ranking or a share of coverage; a rule of one country is
not another country's. Do not work out dates, totals, rankings or labels ("founder", "leading",
"the main") that the output does not state itself.

2. Name, in the answer text itself, where each fact comes from, as the tool output gives it: the
registry, the issuer and document, the outlet or the website -- e.g. "(Companies House, record
extracted 2026-09-27)", "according to Reuters (12 Sep 2026)", "EBA/GL/2021/05, paragraph 98". When
the output attributes different points to different documents or issuers, keep each point with
its own.

3. Carry each date with its data: every status, figure, price, product, appointment and rule comes
with the date or period the output gives for it -- the record or extraction date, the
publication date, the "as of" date, the period a figure refers to, the date from which the
version you describe applies. For questions about news or media coverage, say which window the
articles you found span (oldest to newest date) and how many of them each point rests on.

4. Give the identifiers the output contains for the entity asked about (LEI, registry number, tax
ID, series code), exactly as written there.

5. If the output does not have something the question asks for, say so for that item rather than
filling it in.
"""

_USER = """<question>
{question}
</question>

You have already called {vendor}'s tools for this question; their outputs, in the order you called
them, are below. Do not call any tool. Write the final answer now, from these outputs only,
following the rules of your instructions.

<tool_outputs>
{evidence}
</tool_outputs>

Respond with ONLY this JSON object:
{{"answer": "the complete final answer text"}}
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("ground_truth")
    parser.add_argument("--arm", choices=("control", "rules"), required=True)
    parser.add_argument("--ids", default=None)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="the agent's model (BEDROCK_MODEL_ID of the agent runtime)")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--output", required=True, help=".jsonl to write")
    parser.add_argument("--retry-errors", action="store_true", help="keep the rows of --output that worked, re-write only the failed ones")
    args = parser.parse_args()

    ids = set(args.ids.split(",")) if args.ids else None
    out_path = Path(args.output)
    kept: list[dict] = []
    if args.retry_errors:
        previous = [json.loads(line) for line in open(out_path, encoding="utf-8") if line.strip()]
        kept = [r for r in previous if not r.get("error")]
        ids = {r["id"] for r in previous if r.get("error")}
        if not ids:
            print("no failed rows to retry")
            return
    rows = load_rows([args.ground_truth], ids)
    bedrock = boto3.client("bedrock-runtime", region_name=REGION, config=BotoConfig(read_timeout=600, retries={"max_attempts": 4, "mode": "adaptive"}))
    # Sonnet 5 rejects temperature; the agent runs older models at 0.
    temperature = None if "sonnet-5" in args.model else 0
    print(f"{len(rows)} answer(s), arm {args.arm}, model {args.model}", flush=True)

    def run(record: dict) -> dict:
        candidate = candidate_from(record)
        system = direct_agent._build_system_prompt("ask", candidate.vendor, candidate.vendor, candidate.question)
        if args.arm == "rules":
            system += "\n" + WRITING_RULES
        prompt = _USER.format(question=candidate.question, vendor=candidate.vendor, evidence=evidence_text(candidate))
        out = dict(record, rewrite_arm=args.arm, original_answer=record["answer"])
        try:
            raw = converse_json(bedrock, model_id=args.model, system=system, prompt=prompt, max_tokens=12000, temperature=temperature)
            answer = str(raw.get("answer") or "").strip()
            if not answer:
                raise ValueError("empty answer")
            out["answer"] = answer
        except Exception as exc:  # noqa: BLE001 -- the row is dropped by load_rows downstream
            out.update(ok=False, error=f"{type(exc).__name__}: {exc}")
        return out

    lock = Lock()
    with open(out_path, "w", encoding="utf-8") as fj, ThreadPoolExecutor(max_workers=args.workers) as pool:
        for row in kept:
            fj.write(json.dumps(row, ensure_ascii=False) + "\n")
        futures = [pool.submit(run, record) for record, _ in rows]
        for i, future in enumerate(as_completed(futures), start=1):
            row = future.result()
            with lock:
                fj.write(json.dumps(row, ensure_ascii=False) + "\n")
                fj.flush()
            status = row.get("error") or f"{len(row['original_answer'])} -> {len(row['answer'])} chars"
            print(f"[{i}/{len(rows)}] id={row['id']} {status[:100]}", flush=True)

    grades = Path(args.ground_truth).with_suffix(".csv")
    if grades.exists():
        shutil.copyfile(grades, out_path.with_suffix(".csv"))
    print(f"\nWrote {out_path}" + (f" (POC grades of the original answers copied to {out_path.with_suffix('.csv')})" if grades.exists() else ""))


if __name__ == "__main__":
    main()
