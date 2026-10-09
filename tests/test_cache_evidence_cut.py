"""Tests for the evidence cut to the cited pages (evidence.cited_evidence_text,
cap_sections), measured offline before it is used by admission."""

from __future__ import annotations

import json
from types import SimpleNamespace

from a2k.cache.evidence import cap_sections, cited_evidence_text, evidence_text

CITED = "https://www.eba.europa.eu/gl/2021-05.pdf"
OTHER = "https://example.com/other"


def _mcp(payload) -> dict:
    return {"status": "success", "content": [{"text": json.dumps(payload)}]}


def _candidate(tool_calls, cited=(CITED,)):
    return SimpleNamespace(tool_calls=tool_calls, citations=[{"sourceUrl": u} for u in cited])


SEARCH = {"vendor": "linkup", "toolName": "linkup-search", "input": {"query": "EBA risk culture"},
          "output": _mcp({"results": [{"name": "GL", "url": CITED + "#p3", "content": "Published 2 July 2021. Risk culture..."},
                                      {"name": "Other", "url": OTHER, "content": "unrelated " * 50}]})}
FETCH_CITED = {"vendor": "linkup", "toolName": "linkup-fetch", "input": {"url": CITED}, "output": _mcp({"markdown": "Full guideline text"})}
FETCH_OTHER = {"vendor": "linkup", "toolName": "linkup-fetch", "input": {"url": OTHER}, "output": _mcp({"markdown": "page nobody cites " * 100})}
ENTITY = {"vendor": "cala", "toolName": "entity_retrieval", "input": {"id": "x"}, "output": _mcp({"name": "Banco Santander", "lei": "5493006QMFDDMYWIAM13"})}


def test_uncited_results_and_fetches_are_dropped_cited_ones_kept():
    text, how = cited_evidence_text(_candidate([SEARCH, FETCH_CITED, FETCH_OTHER]))
    assert how == {"mode": "cited", "cited": 1, "found": 1}
    assert "Published 2 July 2021" in text and "Full guideline text" in text
    assert "unrelated" not in text and "page nobody cites" not in text
    assert "(page not cited by the answer: omitted)" in text
    assert len(text) < len(evidence_text(_candidate([SEARCH, FETCH_CITED, FETCH_OTHER])))


def test_data_without_urls_stays_whole():
    text, _ = cited_evidence_text(_candidate([SEARCH, ENTITY]))
    assert "5493006QMFDDMYWIAM13" in text


def test_falls_back_to_the_full_text_when_nothing_cited_is_found():
    c = _candidate([SEARCH], cited=("https://nowhere.example/x",))
    text, how = cited_evidence_text(c)
    assert how["mode"] == "full" and text == evidence_text(c)
    text, how = cited_evidence_text(_candidate([SEARCH], cited=()))
    assert how == {"mode": "full", "reason": "the answer cites no URL"}


def test_cap_sections_cuts_each_tool_call_separately():
    text = "### Tool call 1: a\n" + "x" * 100 + "\n\n### Tool call 2: b\nshort"
    capped = cap_sections(text, 50)
    first, second = capped.split("\n\n### Tool call ")
    assert len(first) == 50 + len("\n[... cut]") and second == "2: b\nshort"
