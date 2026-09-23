"""Standalone diagnostic: shows exactly why a query pair would or wouldn't hit
the semantic answer cache (a2k/gateway/cache.py), without needing a live
a2k-box call or DynamoDB access -- just Bedrock (Titan Embed v2 for the
cosine step, Claude for the equivalence step, same models/prompts
lookup() itself uses).

Imports cache.py's own (underscore-prefixed) internals directly rather than
reimplementing the embedding/cosine/prompt logic -- a second copy of that
logic would silently drift from the real thing over time, which defeats the
point of a diagnostic tool.

Runs BOTH gates lookup() runs, in order, so you can see which one a given
pair actually fails on:
  1. Cosine-similarity pre-filter (config.answer_cache_similarity_threshold,
     0.90 by default) -- cheap, deliberately generous.
  2. LLM equivalence check (_questions_equivalent) -- the real precision
     filter. A pair can clear (1) and still fail (2), e.g. "who's the
     president of Santander" vs "... in Spain" -- same topic, cosine-close,
     but arguably different questions with different correct answers.

Usage:
    python check_cache_similarity.py "first question" "second question"

Needs AWS credentials with bedrock:InvokeModel + bedrock:Converse in
whatever region AWS_REGION points at (same requirement every script in this
directory already has) -- e.g. `aws sso login` first if that's how this
account authenticates.
"""

from __future__ import annotations

import os
import sys

# Run from anywhere: put the repo root (two levels up from deploy/agentcore/)
# on sys.path so `import a2k...` works without installing the package first.
_REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

import boto3  # noqa: E402

from a2k.config import config  # noqa: E402
from a2k.gateway.cache import (  # noqa: E402
    _cosine_similarity,
    _normalize_query,
    _questions_equivalent,
    embed_query,
)

REGION = os.environ.get("AWS_REGION", "eu-west-1")


def main() -> None:
    if len(sys.argv) != 3:
        print(f"Usage: python {os.path.basename(__file__)} \"query one\" \"query two\"", file=sys.stderr)
        raise SystemExit(2)

    query_a, query_b = sys.argv[1], sys.argv[2]
    norm_a, norm_b = _normalize_query(query_a), _normalize_query(query_b)

    bedrock = boto3.client("bedrock-runtime", region_name=REGION)

    print(f"embedding model : {config.answer_cache_embedding_model_id}")
    print(f"verify model    : {config.answer_cache_verify_model_id}")
    print(f"region          : {REGION}")
    print()
    print(f"query A (normalized): {norm_a!r}")
    print(f"query B (normalized): {norm_b!r}")
    print()

    embedding_a = embed_query(bedrock, norm_a)
    embedding_b = embed_query(bedrock, norm_b)
    similarity = _cosine_similarity(embedding_a, embedding_b)
    threshold = config.answer_cache_similarity_threshold
    clears_threshold = similarity >= threshold

    print(f"cosine similarity   : {similarity:.4f}")
    print(f"similarity threshold: {threshold:.4f}")
    print(f"clears pre-filter?  : {'YES' if clears_threshold else 'no'}")
    print()

    if not clears_threshold:
        print("Would MISS at the cosine pre-filter -- lookup() would stop here")
        print("and never reach the equivalence check below (shown anyway, for reference).")
        print()

    equivalent = _questions_equivalent(bedrock, cached_query=norm_a, new_query=norm_b)
    print(f"LLM equivalence check: {'equivalent' if equivalent else 'NOT equivalent'}")
    print()

    would_hit = clears_threshold and equivalent
    print(f"=> overall: {'would HIT the cache' if would_hit else 'would MISS the cache'}")


if __name__ == "__main__":
    main()
