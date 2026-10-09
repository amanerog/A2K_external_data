"""Tokens and estimated cost of the cache's own model calls (E7.4; the cost
side of D14's net cost).

Every Bedrock call of the cache goes through bedrock.converse_json,
bedrock.embed or checker._converse, and each reports its token counts here.
They add up in the Usage of the current context: admission.admit and
retrieval.lookup each open one (track()) around their work, and the
reformulation check, which runs in its own thread, is submitted with a copy
of the lookup's context so its calls land in the same Usage.

The cost is an estimate: tokens times a per-model price per million tokens.
The defaults below are list prices; CACHE_MODEL_PRICES (JSON,
{"model id substring": [input, output]}) overrides or adds to them -- set it
if the account pays other prices (e.g. a regional premium on the eu.
inference profiles). A model with no price adds its tokens but no cost, and
is named in `unpriced`.
"""

from __future__ import annotations

import contextvars
import threading
from contextlib import contextmanager
from typing import Any, Iterator

from ..config import config

# USD per million tokens (input, output), matched by substring of the model id.
DEFAULT_PRICES: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-sonnet-5": (3.0, 15.0),
    "titan-embed-text-v2": (0.02, 0.0),
}


def price_for(model_id: str) -> tuple[float, float] | None:
    prices = {**DEFAULT_PRICES, **config.cache_model_prices}
    # The longest matching key wins, so a specific override beats a family.
    matches = [k for k in prices if k in model_id]
    return prices[max(matches, key=len)] if matches else None


class Usage:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.by_model: dict[str, list[int]] = {}

    def add(self, model_id: str, input_tokens: int, output_tokens: int) -> None:
        with self._lock:
            counts = self.by_model.setdefault(model_id, [0, 0])
            counts[0] += int(input_tokens or 0)
            counts[1] += int(output_tokens or 0)

    def summary(self) -> dict[str, Any]:
        with self._lock:
            by_model = {m: {"input_tokens": i, "output_tokens": o} for m, (i, o) in self.by_model.items()}
        cost, unpriced = 0.0, []
        for model_id, counts in by_model.items():
            price = price_for(model_id)
            if price is None:
                unpriced.append(model_id)
                continue
            cost += (counts["input_tokens"] * price[0] + counts["output_tokens"] * price[1]) / 1_000_000
        return {
            "input_tokens": sum(c["input_tokens"] for c in by_model.values()),
            "output_tokens": sum(c["output_tokens"] for c in by_model.values()),
            "cost_usd": round(cost, 6),
            "by_model": by_model,
            "unpriced": unpriced,
        }


_current: contextvars.ContextVar[Usage | None] = contextvars.ContextVar("a2k_cache_usage", default=None)


@contextmanager
def track() -> Iterator[Usage]:
    """Collects the model calls made inside the block (and in threads started
    with a copy of its context)."""
    usage = Usage()
    token = _current.set(usage)
    try:
        yield usage
    finally:
        _current.reset(token)


def current() -> Usage | None:
    return _current.get()


def record_converse(model_id: str, response: dict[str, Any]) -> None:
    usage = _current.get()
    if usage is not None:
        counts = response.get("usage") or {}
        usage.add(model_id, counts.get("inputTokens", 0), counts.get("outputTokens", 0))


def record_embedding(model_id: str, input_tokens: int) -> None:
    usage = _current.get()
    if usage is not None:
        usage.add(model_id, input_tokens, 0)
