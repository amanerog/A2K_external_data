"""Shared LLM-as-judge helper -- used by run_ground_truth_eval.py (grades
answers right after calling a2k.ask) and grade_vendor_audit.py (grades
answers already captured in a run_vendor_audit.py .jsonl file, no new
AgentCore calls needed). Split out here once a second script needed the
exact same grading logic, rather than duplicate it a second time.

`expected_answer` is free-form prose written by a human, and our system's
`answer` is free-form prose synthesized by an LLM -- these will essentially
never match verbatim even when the system is factually right (different
wording, different level of detail, different ordering of facts). So each
row is graded by a second LLM call (Bedrock Converse) that compares actual
vs. expected on *factual* consistency, not string equality.
"""

from __future__ import annotations

import json

# Same example model used throughout agent/README.md's "Run locally" -- override with
# --judge-model on either caller script if your account's Bedrock access differs.
DEFAULT_JUDGE_MODEL_ID = "eu.anthropic.claude-sonnet-4-5-20250929-v1:0"

JUDGE_PROMPT = """You are grading whether an AI system's answer to a question \
adequately matches a reference (expected) answer. This is NOT a request for \
exact wording match -- different phrasing, different level of detail, or \
additional/fewer specifics than the reference is fine as long as the core \
facts asserted are consistent and not contradicted.

Question: {query}

Reference (expected) answer:
{expected_answer}

System's actual answer:
{actual_answer}

Grade the system's actual answer against the reference on this scale:
- "PASS": the actual answer's key facts are consistent with and substantially
  cover the reference answer's key facts (paraphrasing/formatting/ordering
  differences are fine).
- "PARTIAL": the actual answer captures some but not all of the reference's
  key facts, or is materially less specific/complete, but does not
  contradict it.
- "FAIL": the actual answer contradicts the reference, is substantially
  wrong, or contains none of the reference's key facts -- including cases
  where the system reports it has no information / insufficient evidence
  while the reference shows real information was available.

Respond with ONLY a JSON object, no other text, in exactly this shape:
{{"verdict": "PASS" | "PARTIAL" | "FAIL", "rationale": "<one or two sentences>"}}
"""


def judge(bedrock_client, model_id: str, query: str, expected_answer: str, actual_answer: str) -> tuple[str, str]:
    prompt = JUDGE_PROMPT.format(query=query, expected_answer=expected_answer, actual_answer=actual_answer)
    response = bedrock_client.converse(
        modelId=model_id,
        messages=[{"role": "user", "content": [{"text": prompt}]}],
        inferenceConfig={"maxTokens": 500, "temperature": 0},
    )
    text = response["output"]["message"]["content"][0]["text"].strip()
    # Models occasionally wrap the JSON in a ```json fence despite the
    # "ONLY a JSON object" instruction -- strip that before parsing rather
    # than failing the whole row over a cosmetic formatting slip.
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    parsed = json.loads(text)
    return parsed["verdict"], parsed.get("rationale", "")
