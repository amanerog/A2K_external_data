"""Batch false-negative / recall check for the semantic answer cache
(a2k/gateway/cache.py), the mirror image of check_cache_false_positives.py --
driven by an xlsx test set shaped like Set_Pruebas_Cache_Falsos_Negativos_v1.xlsx
(a "Cache_recall_test_set" sheet, same column shape check_cache_false_positives.py's
_load_rows() already handles: one "Pregunta real (cacheada)" row per group followed by
that group's paraphrase rows).

Every row in this test set is, by construction, a paraphrase of its group's real
question -- reordered, passive voice, synonyms, more/less formal, ... -- worded
differently but asking for exactly the same thing. The correct outcome for every single
one is "WOULD hit the cache" (clears_threshold=True AND llm_equivalent=True). A row
where either gate fails is a genuine false negative: the cache would re-run the full
live agent against a vendor for a question it had already answered, wasting the call
(and the cache's whole reason to exist) rather than a precision problem.

Reuses check_cache_false_positives.py's _load_rows() as-is (same file shape, only the
non-real Tipo label and the pattern column's header text differ, and _load_rows()
already doesn't hardcode either) -- only the per-row failure criterion and the report
labels are inverted here; the two Bedrock gates being checked are identical.

Usage:
    pip install openpyxl  # one-off, not a project dependency
    python check_cache_false_negatives.py Set_Pruebas_Cache_Falsos_Negativos_v1.xlsx

Needs AWS credentials with bedrock:InvokeModel + bedrock:Converse, same as
check_cache_similarity.py, in whatever region AWS_REGION points at.
"""

from __future__ import annotations

import csv
import os
import sys

_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import boto3  # noqa: E402

from a2k.config import config  # noqa: E402
from a2k.gateway.cache import (  # noqa: E402
    _EQUIVALENCE_PROMPT,
    _converse_json,
    _cosine_similarity,
    _normalize_query,
    embed_query,
)
from check_cache_false_positives import REAL_TYPE, _load_rows  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-west-1")


def main() -> None:
    if len(sys.argv) < 2:
        print(f"Usage: python {os.path.basename(__file__)} <test-set.xlsx> [--sheet Cache_recall_test_set] [--output report.csv]", file=sys.stderr)
        raise SystemExit(2)

    xlsx_path = sys.argv[1]
    sheet = "Cache_recall_test_set"
    output_path = os.path.splitext(os.path.basename(xlsx_path))[0] + "_report.csv"
    args = sys.argv[2:]
    while args:
        flag, args = args[0], args[1:]
        if flag == "--sheet":
            sheet, args = args[0], args[1:]
        elif flag == "--output":
            output_path, args = args[0], args[1:]
        else:
            print(f"Unrecognized argument: {flag}", file=sys.stderr)
            raise SystemExit(2)

    rows = _load_rows(xlsx_path, sheet)
    paraphrase_rows = [r for r in rows if r["tipo"] != REAL_TYPE]
    print(f"Loaded {len(rows)} row(s) ({len(paraphrase_rows)} paraphrase pairs to test) from {xlsx_path!r}, sheet {sheet!r}")
    print(f"Threshold: {config.answer_cache_similarity_threshold:.2f}  |  Embedding model: {config.answer_cache_embedding_model_id}  |  Verify model: {config.answer_cache_verify_model_id}")
    print()

    bedrock = boto3.client("bedrock-runtime", region_name=REGION)
    threshold = config.answer_cache_similarity_threshold

    results = []
    false_negatives = 0
    for i, r in enumerate(paraphrase_rows, 1):
        real_q = _normalize_query(r["real_question"])
        new_q = _normalize_query(r["question"])

        embedding_real = embed_query(bedrock, real_q)
        embedding_new = embed_query(bedrock, new_q)
        similarity = _cosine_similarity(embedding_real, embedding_new)
        clears_threshold = similarity >= threshold

        # Unlike check_cache_false_positives.py, still run the equivalence
        # check even when clears_threshold is False -- for a false positive,
        # failing the cosine pre-filter alone is enough to know the pair is
        # correctly rejected (no need to know what the LLM would've said).
        # For recall, failing the pre-filter *is itself* the false negative
        # we're trying to characterize -- worth knowing whether the LLM gate
        # would separately have caught it right, as a diagnostic on which
        # gate is the weaker link for this pair.
        prompt = _EQUIVALENCE_PROMPT.format(cached_query=real_q, new_query=new_q)
        parsed = _converse_json(bedrock, prompt)
        equivalent = bool(parsed.get("equivalent", False))
        reason = parsed.get("reason", "")

        would_hit = clears_threshold and equivalent
        is_false_negative = not would_hit
        if is_false_negative:
            false_negatives += 1

        status = "FALSE NEGATIVE (would miss)" if is_false_negative else "ok (correctly hit)"
        print(f"[{i}/{len(paraphrase_rows)}] #{r['n']} grupo={r['group']} ({r['pattern']}): cos={similarity:.4f} equivalent={equivalent} -> {status}")

        results.append(
            {
                "n": r["n"],
                "grupo": r["group"],
                "tema": r["tema"],
                "idioma": r["idioma"],
                "patron_parafraseo": r["pattern"],
                "pregunta_real": r["real_question"],
                "pregunta_parafraseada": r["question"],
                "cosine_similarity": f"{similarity:.4f}",
                "clears_threshold": clears_threshold,
                "llm_equivalent": equivalent,
                "llm_reason": reason,
                "falso_negativo": is_false_negative,
            }
        )

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(results[0].keys()))
        writer.writeheader()
        writer.writerows(results)

    print()
    print(f"=== Summary: {false_negatives}/{len(paraphrase_rows)} false negative(s) -- report written to {output_path}")


if __name__ == "__main__":
    main()
