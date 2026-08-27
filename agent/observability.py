"""Structured logs + CloudWatch metrics for the router agent (agent/core.py),
printed as JSON lines to stdout -- same convention as a2k-box's own
gateway/audit.py and gateway/tracing.py, so these land in CloudWatch Logs for
the deployed AgentCore Runtime with no extra plumbing.

Two kinds of line, both plain `print(json.dumps(...))`:

- **Tool-call logs** (`ToolCallLogger`): one per tool invocation, hooked via
  Strands' typed hook events (BeforeToolCallEvent/AfterToolCallEvent --
  https://strandsagents.com/docs/user-guide/concepts/agents/hooks/) rather
  than wrapping the MCP client call directly, so `chosen_vendor` reflects
  what the *model* actually chose to pass as the `sources` argument, not
  just what went over the wire underneath it.
- **Query metrics** (`emit_query_metrics`): one CloudWatch Embedded Metric
  Format (EMF) line per `core.ask()` call. EMF piggybacks on the same
  stdout -> CloudWatch Logs path as the log lines above -- CloudWatch
  auto-extracts the named metrics from the log line, so this needs no extra
  IAM permission beyond the `logs:*` the AgentCore Runtime execution role
  already needs (see deploy/agentcore/README.md's IAM troubleshooting
  section) and no separate `cloudwatch:PutMetricData` call on the request
  path.

Known approximations, spelled out here rather than left implicit:

- `tool_http_status`: Strands' ToolResult only carries a "success"/"error"
  status (strands/types/tools.py), not a real HTTP status code -- MCP tool
  results don't surface one to the agent. Mapped to 200/502 as the closest
  honest proxy (502, not 500: a tool failure here is almost always the
  Gateway/a2k-box/upstream vendor failing, not this agent) unless the
  underlying exception is an httpx.HTTPStatusError, in which case its real
  status code is used instead.
- `ClientCount`: emitted as 1 per query, dimensioned by `internal_client`.
  That's a per-call counter you SUM/group-by `internal_client` in CloudWatch
  to see volume per client -- not a running distinct-client gauge. A
  stateless request handler has no cross-invocation memory to count
  distinct clients itself; for a genuine distinct-client count, run
  CloudWatch Contributor Insights against the `internal_client` field on
  the log lines this module emits.
"""

from __future__ import annotations

import json
import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

from strands.hooks import AfterToolCallEvent, BeforeToolCallEvent, HookRegistry

_METRICS_NAMESPACE = os.environ.get("A2K_AGENT_METRICS_NAMESPACE", "A2K/RouterAgent")

# Suffix match, not equality -- same reasoning as core.py's _LIST_VENDORS_MCP_NAME:
# through the Gateway, tool names carry a "<target-name>___" prefix, and core.py's
# own name_override replaces every "." with "_" (Bedrock's Converse API tool-name
# charset), so the a2k.ask tool the model actually calls shows up as
# "<target>___a2k_ask", never the bare "a2k.ask"/"a2k_ask".
_ASK_TOOL_SUFFIX = "a2k_ask"


def _now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _emit(record: dict[str, Any]) -> None:
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)


def _tool_http_status(result: dict[str, Any] | None, exception: Exception | None) -> int:
    if exception is not None:
        response = getattr(exception, "response", None)  # httpx.HTTPStatusError shape
        status_code = getattr(response, "status_code", None)
        if isinstance(status_code, int):
            return status_code
        return 502
    if result is not None and result.get("status") == "success":
        return 200
    return 502


class ToolCallLogger:
    """Strands HookProvider -- emits one `agent.tool_call` log line per tool
    call, and tallies invocation/error counts for `emit_query_metrics`.

    A fresh instance is built per `ask()` call (see core.py) rather than
    reused across requests: `session_id`/`internal_client` are only known to
    that call, not to Strands' event objects, and the tally needs to start
    at zero for each query anyway.
    """

    def __init__(
        self, *, session_id: str | None, internal_client: str | None, verbose: bool = False
    ) -> None:
        self._session_id = session_id
        self._internal_client = internal_client
        self._verbose = verbose
        self._start_times: dict[str, str] = {}  # toolUseId -> ISO timestamp captured in _before
        self.invocation_count = 0
        self.error_count = 0

    def register_hooks(self, registry: HookRegistry) -> None:
        registry.add_callback(BeforeToolCallEvent, self._before)
        registry.add_callback(AfterToolCallEvent, self._after)

    def _before(self, event: BeforeToolCallEvent) -> None:
        self._start_times[event.tool_use["toolUseId"]] = _now_iso()
        if self._verbose:
            # This is what gets sent to *our* MCP (a2k-box, through the Gateway) --
            # not Sayari's/Cala's own MCP underneath it. a2k-box's Runtime is a
            # separate process (remote, on AgentCore), so what *it* then sends to
            # Sayari/Cala isn't observable from here -- see adapters/sayari_mcp.py's
            # `_call_tool` tracing (A2K_TRACE_CALLS, CloudWatch) or
            # test_sayari_probe.py (runs the adapter locally) for that side.
            print(f">> [our MCP] tool called: {event.tool_use.get('name')}")
            print(f"   arguments:  {json.dumps(event.tool_use.get('input'), ensure_ascii=False)}")

    def _after(self, event: AfterToolCallEvent) -> None:
        tool_use_id = event.tool_use["toolUseId"]
        tool_end_time = _now_iso()
        # Falls back to tool_end_time if BeforeToolCallEvent never fired for this
        # toolUseId (shouldn't happen -- Strands always pairs the two -- but a
        # missing start shouldn't crash logging over a cosmetic timestamp).
        tool_start_time = self._start_times.pop(tool_use_id, tool_end_time)

        result = event.result if isinstance(event.result, dict) else None
        status = _tool_http_status(result, event.exception)
        errored = event.exception is not None or (result is not None and result.get("status") != "success")

        self.invocation_count += 1
        if errored:
            self.error_count += 1

        chosen_vendor = None
        tool_name = event.tool_use.get("name", "")
        if tool_name.endswith(_ASK_TOOL_SUFFIX):
            tool_input = event.tool_use.get("input") or {}
            # `sources` omitted/empty means "fan out to all active vendors" (see
            # core.py's system prompt, rule 4) -- logged as None, not "[]", so a
            # dashboard can tell "explicit fan-out" apart from "not an ask call".
            chosen_vendor = tool_input.get("sources") or None

        error_type = None
        if event.exception is not None:
            error_type = type(event.exception).__name__
        elif errored:
            error_type = "ToolError"

        if self._verbose:
            if event.exception is not None:
                print(f"<< [our MCP] raised: {error_type}: {event.exception}")
            else:
                print(f"<< [our MCP] result:  {json.dumps(result, ensure_ascii=False)}")

        _emit(
            {
                "event": "agent.tool_call",
                "agent_timestamp": tool_end_time,
                "session_id": self._session_id,
                "chosen_vendor": chosen_vendor,
                "tool_start_time": tool_start_time,
                "tool_end_time": tool_end_time,
                "tool_http_status": status,
                "error_type": error_type,
                "internal_client": self._internal_client,
            }
        )


def emit_query_metrics(
    *,
    internal_client: str | None,
    latency_ms: float,
    invocation_count: int,
    error_count: int,
    query_errored: bool,
) -> None:
    """One CloudWatch EMF line per `core.ask()` call -- QueryCount, ErrorCount,
    Latency, InvocationCount, ClientCount, dimensioned by `internal_client`."""
    dimension_value = internal_client or "unknown"
    # ErrorCount is query-level (0 or 1), matching QueryCount's "one row per
    # query" grain -- a query counts as errored if the whole call raised, or
    # any individual tool call inside it errored. Which tool call and why is
    # already in that tool call's own agent.tool_call log line (error_type);
    # summing per-tool errors in here too would double-count against that.
    query_had_error = query_errored or error_count > 0
    _emit(
        {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [
                    {
                        "Namespace": _METRICS_NAMESPACE,
                        "Dimensions": [["internal_client"]],
                        "Metrics": [
                            {"Name": "QueryCount", "Unit": "Count"},
                            {"Name": "ErrorCount", "Unit": "Count"},
                            {"Name": "Latency", "Unit": "Milliseconds"},
                            {"Name": "InvocationCount", "Unit": "Count"},
                            {"Name": "ClientCount", "Unit": "Count"},
                        ],
                    }
                ],
            },
            "internal_client": dimension_value,
            "QueryCount": 1,
            "ErrorCount": int(query_had_error),
            "Latency": latency_ms,
            "InvocationCount": invocation_count,
            "ClientCount": 1,
        }
    )
