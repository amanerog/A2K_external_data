"""Cache metrics (E7.4) as CloudWatch Embedded Metric Format: one extra
stdout line (`event: cache.metrics`) next to each cache.retrieval and
cache.admission log line, which CloudWatch turns into metrics in
config.cache_metrics_namespace -- no PutMetricData calls, no extra
permissions. The log lines themselves are unchanged; the metrics are what
the dashboard and the alarms (E7.5) read, the log lines what Logs Insights
breaks down further.

Retrieval, one per ask:
  by Mode -- Asks, Hits (served from the cache), WouldHits, Errors, Skipped
  (cache bypassed), Reformulations, OverBudget, RetrievalMs, and InputTokens,
  OutputTokens, CostUSD of its model calls (usage.py);
  by Mode+Domain -- Asks, Hits, WouldHits only;
  by ServedFrom (cache / vendor / vendor_error / vendor_no_answer) -- AskMs,
  the whole ask as a2k-box saw it, to compare a hit with a vendor answer.
Admission, one per candidate:
  by Outcome -- Admissions, AdmissionMs, InputTokens, OutputTokens, CostUSD;
  by Domain+Outcome and Vendor+Outcome -- Admissions only.
Cache size (`source: cache.size`), at most every config.cache_size_metric_seconds,
after an admission (so an idle, paused Aurora is never woken for it):
  Entries by State (PENDING / ACTIVE / EXPIRED / DEPRECATED), and by
  Domain+State -- one line per row, read with Maximum, not Sum.
Shadow check (shadow_check.py), one per would-be hit compared:
  by Verdict -- ShadowChecks; by Domain -- ShadowChecks, FalseHits; and
  CostUSD of the comparison.

Every dimension combination is a billed custom metric, so the per-domain and
per-vendor ones are kept to the counts; the dashboard's per-domain tables
come from Logs Insights over these same lines.

A question with no domain (bypassed, or retrieval failed before classifying)
is counted under Domain "NONE".
"""

from __future__ import annotations

import threading
import time
from typing import Any

from ..config import config
from ..logline import emit

NO_DOMAIN = "NONE"


def _count(name: str) -> dict[str, str]:
    return {"Name": name, "Unit": "Count"}


def _ms(name: str) -> dict[str, str]:
    return {"Name": name, "Unit": "Milliseconds"}


def _envelope(namespace: str, directives: list[tuple[list[list[str]], list[dict[str, str]]]], values: dict[str, Any]) -> dict[str, Any]:
    return {
        "_aws": {
            "Timestamp": int(time.time() * 1000),
            "CloudWatchMetrics": [{"Namespace": namespace, "Dimensions": dims, "Metrics": metrics} for dims, metrics in directives],
        },
        "event": "cache.metrics",
        **values,
    }


def retrieval_metrics(record: dict[str, Any], namespace: str) -> dict[str, Any]:
    """The EMF line for one cache.retrieval record (engine._log_retrieval)."""
    domains = record.get("domains") or []
    reformulation = record.get("reformulation") or {}
    values: dict[str, Any] = {
        "source": "cache.retrieval",
        "Mode": record.get("mode") or "off",
        "Domain": domains[0] if domains else NO_DOMAIN,
        "ServedFrom": record.get("served_from") or "vendor",
        "Asks": 1,
        "Hits": int(record.get("served_from") == "cache"),
        "WouldHits": int(bool(record.get("would_hit"))),
        "Errors": int(record.get("error") is not None),
        "Skipped": int(record.get("skipped") is not None),
        "Reformulations": int(reformulation.get("kind") == "reformulation"),
        "OverBudget": int(record.get("within_budget") is False),
        "RetrievalMs": record.get("total_ms") or 0,
    }
    per_ask = [_count(n) for n in ("Asks", "Hits", "WouldHits", "Errors", "Skipped", "Reformulations", "OverBudget")] + [_ms("RetrievalMs")]
    per_ask += _usage(values, record.get("usage"))
    directives = [([["Mode"]], per_ask), ([["Mode", "Domain"]], [_count(n) for n in ("Asks", "Hits", "WouldHits")])]
    if record.get("ask_ms") is not None:
        values["AskMs"] = record["ask_ms"]
        directives.append(([["ServedFrom"]], [_ms("AskMs")]))
    return _envelope(namespace, directives, values)


def admission_metrics(record: dict[str, Any], namespace: str) -> dict[str, Any]:
    """The EMF line for one cache.admission record (admission.log_result)."""
    domains = record.get("domains") or []
    values = {
        "source": "cache.admission",
        "Outcome": record.get("outcome") or "ERROR",
        "Domain": domains[0] if domains else NO_DOMAIN,
        "Vendor": record.get("vendor") or "unknown",
        "Admissions": 1,
        "AdmissionMs": sum((record.get("timings_ms") or {}).values()),
    }
    per_outcome = [_count("Admissions"), _ms("AdmissionMs")] + _usage(values, record.get("usage"))
    return _envelope(namespace, [([["Outcome"]], per_outcome), ([["Domain", "Outcome"], ["Vendor", "Outcome"]], [_count("Admissions")])], values)


def _usage(values: dict[str, Any], usage: dict[str, Any] | None) -> list[dict[str, str]]:
    """Adds the tokens and cost of a usage summary to `values`; returns their
    metric definitions (none without a summary)."""
    if not usage:
        return []
    values.update(InputTokens=usage["input_tokens"], OutputTokens=usage["output_tokens"], CostUSD=usage["cost_usd"])
    return [_count("InputTokens"), _count("OutputTokens"), {"Name": "CostUSD", "Unit": "None"}]


def size_metrics(rows: list[dict[str, Any]], namespace: str) -> list[dict[str, Any]]:
    """EMF lines for store.size(): one per state with the total, one per
    domain and state."""
    totals: dict[str, int] = {}
    for row in rows:
        totals[row["state"]] = totals.get(row["state"], 0) + row["entries"]
    lines = [_envelope(namespace, [([["State"]], [_count("Entries")])], {"source": "cache.size", "State": s, "Entries": n}) for s, n in sorted(totals.items())]
    lines += [
        _envelope(namespace, [([["Domain", "State"]], [_count("Entries")])], {"source": "cache.size", "Domain": r["domain"], "State": r["state"], "Entries": r["entries"]})
        for r in rows
    ]
    return lines


def shadow_check_metrics(record: dict[str, Any], namespace: str) -> dict[str, Any]:
    """The EMF line for one cache.shadow_check record (shadow_check.run)."""
    values: dict[str, Any] = {
        "source": "cache.shadow_check",
        "Verdict": record.get("verdict") or "ERROR",
        "Domain": record.get("domain") or NO_DOMAIN,
        "ShadowChecks": 1,
        "FalseHits": int(bool(record.get("false_hit"))),
    }
    cost = [m for m in _usage(values, record.get("usage")) if m["Name"] == "CostUSD"]
    return _envelope(namespace, [([["Verdict"]], [_count("ShadowChecks"), *cost]), ([["Domain"]], [_count("ShadowChecks"), _count("FalseHits")])], values)


def emit_retrieval(record: dict[str, Any]) -> None:
    if config.cache_metrics_namespace:
        emit(retrieval_metrics(record, config.cache_metrics_namespace))


def emit_admission(record: dict[str, Any]) -> None:
    if config.cache_metrics_namespace:
        emit(admission_metrics(record, config.cache_metrics_namespace))


def emit_shadow_check(record: dict[str, Any]) -> None:
    if config.cache_metrics_namespace:
        emit(shadow_check_metrics(record, config.cache_metrics_namespace))


_size_lock = threading.Lock()
_size_last = 0.0


def maybe_emit_cache_size(store) -> None:
    """At most every config.cache_size_metric_seconds; never raises."""
    global _size_last
    if not config.cache_metrics_namespace:
        return
    with _size_lock:
        now = time.monotonic()
        if _size_last and now - _size_last < config.cache_size_metric_seconds:
            return
        _size_last = now
    try:
        lines = size_metrics(store.size(), config.cache_metrics_namespace)
    except Exception as exc:  # noqa: BLE001 -- a metric must never break admission
        emit({"event": "cache.size", "error": f"{type(exc).__name__}: {exc}"})
        return
    for line in lines:
        emit(line)
