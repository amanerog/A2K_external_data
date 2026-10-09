"""Bedrock calls shared by the admission steps: a Converse call that must
come back as one JSON object, and the Titan v2 embedding.

JSON-in-text rather than a forced tool call: Claude Sonnet 5 runs with
reasoning on by default (confirmed by deploy/agentcore/judge.py, which sees a
`reasoningContent` block before the text), and forcing a specific tool is
not compatible with reasoning. So the prompt asks for a JSON object, the
response is parsed defensively, and a parse failure gets one retry -- the
POCv3 evaluator lost a row (id 46) to exactly this kind of truncated JSON.
"""

from __future__ import annotations

import json
from typing import Any

from . import usage


class ModelOutputError(ValueError):
    """The model answered, but not with a usable JSON object."""


def _extract_text(response: dict) -> str:
    for block in response["output"]["message"]["content"]:
        if "text" in block:
            return block["text"]
    raise ModelOutputError("no text block in Converse response")


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    # Tolerate a sentence before/after the object: take the outermost braces.
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ModelOutputError(f"no JSON object in model output: {text[:200]!r}")
    try:
        # strict=False: models sometimes put a raw newline or tab inside a string
        # (invalid control character, POCv3 id 32 in basic mode, 2026-10-06).
        parsed = json.loads(text[start : end + 1], strict=False)
    except json.JSONDecodeError as exc:
        # Two objects in a row ("{...}\n{...}", Haiku in retrieval, 2026-10-09):
        # the first complete one is the answer.
        try:
            parsed, _ = json.JSONDecoder(strict=False).raw_decode(text[start:])
        except json.JSONDecodeError:
            raise ModelOutputError(f"invalid JSON in model output: {exc}") from exc
    if not isinstance(parsed, dict):
        raise ModelOutputError("model output is JSON but not an object")
    return parsed


def converse_json(
    bedrock_client,
    *,
    model_id: str,
    prompt: str,
    max_tokens: int,
    attempts: int = 2,
    system: str | None = None,
    temperature: float | None = None,
) -> dict[str, Any]:
    """`prompt` is the user message; `system`, if given, the system prompt.
    `temperature` only where the model accepts it: Sonnet 5 rejects it (see
    deploy/agentcore/judge.py), so the judge never sends it.
    `max_tokens` must leave room for the reasoning block as well as the JSON."""
    inference: dict[str, Any] = {"maxTokens": max_tokens}
    if temperature is not None:
        inference["temperature"] = temperature
    extra: dict[str, Any] = {"system": [{"text": system}]} if system else {}
    last_error: Exception | None = None
    for _ in range(attempts):
        response = bedrock_client.converse(
            modelId=model_id,
            messages=[{"role": "user", "content": [{"text": prompt}]}],
            inferenceConfig=inference,
            **extra,
        )
        usage.record_converse(model_id, response)  # every attempt is paid
        if response.get("stopReason") == "max_tokens":
            last_error = ModelOutputError(f"model output truncated at maxTokens={max_tokens}")
            continue
        try:
            return parse_json_object(_extract_text(response))
        except ModelOutputError as exc:
            last_error = exc
    assert last_error is not None
    raise last_error


def embed(bedrock_client, *, model_id: str, text: str) -> list[float]:
    response = bedrock_client.invoke_model(
        modelId=model_id,
        body=json.dumps({"inputText": text, "dimensions": 1024, "normalize": True}),
    )
    body = json.loads(response["body"].read())
    usage.record_embedding(model_id, body.get("inputTextTokenCount", 0))
    return body["embedding"]
