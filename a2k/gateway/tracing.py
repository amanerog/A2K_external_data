"""Opt-in call tracing (A2K_TRACE_CALLS=true) for debugging the full request
-> upstream-tool-call -> result chain -- added 2026-08-21 after debugging
the Cala relationships fix blind, with zero visibility into what
adapters/cala_mcp.py was actually sending to/receiving from Cala. Off by
default: dumping every call and result to stdout is noisy for normal
operation, and results can be large (see config.max_entities_to_hydrate's
docstring for how large) -- previews are truncated for the same reason, so
this is for tracing the *shape* of a request, not a substitute for the
audit trail (gateway/audit.py) or full response inspection.

Prints JSON lines to stdout, same convention as gateway/audit.py, so
entries show up in CloudWatch Logs for the deployed Runtime with no extra
plumbing -- no separate log sink to wire up.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from typing import Any

from ..config import config

# Keeps a single log line readable and bounds how much a huge upstream
# result (an entity with dozens of relationship types, for instance) can
# inflate one trace event by.
_MAX_PREVIEW_CHARS = 800


def _preview(value: Any) -> str:
    if isinstance(value, str):
        text = value
    else:
        try:
            text = json.dumps(value, default=str)
        except TypeError:
            text = repr(value)
    if len(text) > _MAX_PREVIEW_CHARS:
        return text[:_MAX_PREVIEW_CHARS] + f"...<{len(text)} chars total, truncated>"
    return text


def trace(event: str, **fields: Any) -> None:
    """No-op unless A2K_TRACE_CALLS=true. Call at each hop of a request worth
    following -- engine entry/exit, each upstream MCP tool call and its
    result -- with whatever fields identify that hop (query, sources, tool,
    arguments, result, requestId, ...). Field values are truncated previews,
    not the full data -- this is for "what got called with what", not a
    complete record (use the audit trail or the response itself for that)."""
    if not config.trace_calls:
        return
    record = {
        "trace": True,
        "event": event,
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        **{key: _preview(val) for key, val in fields.items()},
    }
    print(json.dumps(record, default=str), file=sys.stdout, flush=True)
