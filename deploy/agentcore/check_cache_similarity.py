"""Standalone diagnostic: shows why a pair of questions would or wouldn't
match in the cache's retrieval (a2k/cache/retrieval.py), without a live
a2k-box call or Aurora -- just Bedrock, with the same embedding model and
Checker prompts retrieval uses.

Imports the real embedding and Checker functions rather than reimplementing
them -- a second copy would silently drift from the real thing.

Shows the two gates a pair has to clear, in order:
  1. Cosine similarity >= the domain threshold (retrieval_policy, 0.90 for
     every domain to start with, D3). Pass a different one with --threshold.
  2. Checker filter 1, question equivalence -- the real precision filter.
     A pair can clear (1) and still fail (2), e.g. "who's the president of
     Santander" vs "... in Spain". Filter 2 (does the cached answer satisfy
     the new question) needs a real cached answer, so it is not run here.

Retrieval translates questions to English before embedding them; this
script embeds them as typed, so compare questions in the same language.

Usage:
    python check_cache_similarity.py "first question" "second question" [--threshold 0.85]

Needs AWS credentials with bedrock:InvokeModel + bedrock:Converse in
whatever region AWS_REGION points at (same requirement every script in this
directory already has) -- e.g. `aws sso login` first if that's how this
account authenticates.
"""

from __future__ import annotations

import argparse
import math
import os
import sys

# Run from anywhere: put the repo root (two levels up from deploy/agentcore/)
# on sys.path so `import a2k...` works without installing the package first.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import boto3  # noqa: E402

from a2k.cache.bedrock import embed  # noqa: E402
from a2k.cache.checker import questions_equivalent  # noqa: E402
from a2k.config import config  # noqa: E402

REGION = os.environ.get("AWS_REGION", "eu-west-1")


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query_a")
    parser.add_argument("query_b")
    parser.add_argument("--threshold", type=float, default=0.90)
    args = parser.parse_args()
    norm_a, norm_b = args.query_a, args.query_b

    bedrock = boto3.client("bedrock-runtime", region_name=REGION)

    print(f"embedding model : {config.cache_embedding_model_id}")
    print(f"checker model   : {config.cache_checker_model_id}")
    print(f"region          : {REGION}")
    print()
    print(f"query A: {norm_a!r}")
    print(f"query B: {norm_b!r}")
    print()

    embedding_a = embed(bedrock, model_id=config.cache_embedding_model_id, text=norm_a)
    embedding_b = embed(bedrock, model_id=config.cache_embedding_model_id, text=norm_b)
    similarity = _cosine(embedding_a, embedding_b)
    threshold = args.threshold
    clears_threshold = similarity >= threshold

    print(f"cosine similarity   : {similarity:.4f}")
    print(f"similarity threshold: {threshold:.4f}")
    print(f"clears pre-filter?  : {'YES' if clears_threshold else 'no'}")
    print()

    if not clears_threshold:
        print("Would MISS at the vector search -- retrieval would stop here")
        print("and never reach the equivalence check below (shown anyway, for reference).")
        print()

    equivalent, reason = questions_equivalent(bedrock, model_id=config.cache_checker_model_id, cached_query=norm_a, new_query=norm_b)
    print(f"Checker filter 1: {'equivalent' if equivalent else 'NOT equivalent'} -- {reason}")
    print()

    would_hit = clears_threshold and equivalent
    print(f"=> {'passes both gates (filter 2 still applies to a real cached answer)' if would_hit else 'would MISS the cache'}")


if __name__ == "__main__":
    main()
