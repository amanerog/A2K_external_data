"""ModelCallLogger (observability.py): one agent.model_call line per call to
the model, with its time, tokens, stop reason and the tools it asked for.

    cd agent && python -m pytest test_observability_model_calls.py -v
"""

import contextlib
import io
import json
from types import SimpleNamespace

import observability


def _run(logger, message, stop_reason="tool_use", exception=None):
    logger._before(SimpleNamespace())
    stop = SimpleNamespace(message=message, stop_reason=stop_reason) if message is not None else None
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        logger._after(SimpleNamespace(stop_response=stop, exception=exception))
    return json.loads(buf.getvalue().strip().splitlines()[-1])


def test_a_model_call_line_carries_time_tokens_and_requested_tools():
    logger = observability.ModelCallLogger(session_id="req-1", phase="answer", vendor="cala")
    message = {
        "role": "assistant",
        "content": [{"text": "Voy a buscar."}, {"toolUse": {"toolUseId": "t1", "name": "knowledge_search", "input": {}}}],
        "metadata": {"usage": {"inputTokens": 12000, "outputTokens": 150, "cacheReadInputTokens": 9000}, "metrics": {"latencyMs": 2100}},
    }
    line = _run(logger, message)
    assert line["event"] == "agent.model_call" and (line["session_id"], line["phase"], line["vendor"], line["call"]) == ("req-1", "answer", "cala", 1)
    assert (line["input_tokens"], line["output_tokens"], line["cache_read_tokens"], line["bedrock_latency_ms"]) == (12000, 150, 9000, 2100)
    assert line["tools_requested"] == ["knowledge_search"] and line["text_chars"] == len("Voy a buscar.")
    assert line["stop_reason"] == "tool_use" and line["duration_ms"] >= 0 and line["error"] is None


def test_calls_are_numbered_and_a_failed_call_is_logged():
    logger = observability.ModelCallLogger(session_id="req-1", phase="vendor_decision")
    _run(logger, {"content": [{"text": "x"}], "metadata": {}}, stop_reason="end_turn")
    line = _run(logger, None, exception=RuntimeError("throttled"))
    assert line["call"] == 2 and line["error"] == "RuntimeError: throttled" and line["input_tokens"] is None
