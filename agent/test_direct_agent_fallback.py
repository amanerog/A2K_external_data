"""Unit tests for direct_agent.handle()'s vendor fallback (cache proposal task
E4.9 / D17): a vendor whose phase-2 run fails drops out, and if none answer
the caller gets VENDOR_FALLBACK_MESSAGE instead of an error. Network-free:
run_vendor_agent_sync is replaced, and `sources` is passed so phase 1 (the
Bedrock vendor decision) never runs.

    cd agent && python -m pytest test_direct_agent_fallback.py -v
"""

import asyncio

import direct_agent as da

CATALOGUE = [{"sourceId": "cala", "status": "active"}, {"sourceId": "sayari", "status": "active"}]


def _ok(answer):
    return da.VendorCallResult(
        content=da.AskContent(answer=answer, claims=[], citations=[], groundedRatio=1.0),
        invocation_count=1,
        error_count=0,
        usage={"inputTokens": 10, "outputTokens": 5, "totalTokens": 15},
        tool_calls=[{"vendor": "cala", "toolName": "knowledge_search"}],
    )


def _handle(sources):
    return asyncio.run(
        da.handle(operation="ask", query="q", sources=sources, catalogue=CATALOGUE, model_id="m")
    )


def test_unavailable_vendor_returns_the_fixed_message(monkeypatch, capsys):
    def boom(**kw):
        raise ConnectionError("MCP server unreachable")

    monkeypatch.setattr(da, "run_vendor_agent_sync", boom)
    content = _handle(["cala"])
    assert content["answer"] == da.VENDOR_FALLBACK_MESSAGE
    assert content["toolCalls"] == [] and content["claims"] == []
    assert '"event": "agent.vendor_unavailable"' in capsys.readouterr().out


def test_one_failed_vendor_does_not_hide_the_others(monkeypatch):
    def run(**kw):
        if kw["source_id"] == "sayari":
            raise TimeoutError("sayari down")
        return _ok("Cala says X.")

    monkeypatch.setattr(da, "run_vendor_agent_sync", run)
    content = _handle(["cala", "sayari"])
    assert content["answer"] == "Cala says X."


def test_answer_cut_at_max_tokens_never_reaches_the_caller(monkeypatch):
    """The proposal's T2 'cut off' case: Strands raises instead of returning a
    partial structured answer, and that becomes the fallback message -- which
    cache admission then discards by exact match."""
    from strands.types.exceptions import MaxTokensReachedException

    def truncated(**kw):
        raise MaxTokensReachedException(message="Model stopped generating due to maximum token limit.")

    monkeypatch.setattr(da, "run_vendor_agent_sync", truncated)
    assert _handle(["cala"])["answer"] == da.VENDOR_FALLBACK_MESSAGE


def test_phase_1_only_sees_active_vendors(monkeypatch):
    seen = {}

    def decide(query, catalogue, model_id, region):
        seen["ids"] = [v["sourceId"] for v in catalogue]
        return ["cala"]

    monkeypatch.setattr(da, "_decide_vendors_sync", decide)
    monkeypatch.setattr(da, "run_vendor_agent_sync", lambda **kw: _ok("an answer"))
    catalogue = [{"sourceId": "cala", "status": "active"}, {"sourceId": "sayari", "status": "retired"}]
    out = asyncio.run(da.handle(operation="ask", query="q", sources=None, catalogue=catalogue, model_id="m"))
    assert seen["ids"] == ["cala"] and out["answer"] == "an answer"
