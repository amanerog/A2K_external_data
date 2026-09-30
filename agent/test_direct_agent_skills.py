"""Unit tests for direct_agent.py's _vendor_skills_block -- vendor skills are
inlined into the system prompt instead of left to AgentSkills' progressive
disclosure (see the comment above _skill_text_cache for why). Network-free:
_fetch_skill_text's HTTP call is stubbed via httpx.get.

    cd agent && python -m pytest test_direct_agent_skills.py -v
"""

import httpx
import pytest

import direct_agent as da


@pytest.fixture(autouse=True)
def empty_skill_cache(monkeypatch):
    monkeypatch.setattr(da, "_skill_text_cache", {})


class _FakeResponse:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def test_skill_body_is_inlined_verbatim(monkeypatch):
    monkeypatch.setattr(da.httpx, "get", lambda url, **kw: _FakeResponse(f"body of {url}\n"))

    block = da._vendor_skills_block("linkup")

    for url in da.LINKUP_SKILL_URLS:
        assert f'<vendor_skill source="{url}">\nbody of {url}\n</vendor_skill>' in block


def test_skill_is_fetched_once_per_process(monkeypatch):
    calls = []

    def fake_get(url, **kw):
        calls.append(url)
        return _FakeResponse("skill")

    monkeypatch.setattr(da.httpx, "get", fake_get)

    da._vendor_skills_block("cala")
    da._vendor_skills_block("cala")

    assert calls == [da.CALA_SKILL_URL]


def test_vendor_without_skill_gets_no_block(monkeypatch):
    monkeypatch.setattr(da.httpx, "get", lambda url, **kw: pytest.fail("sayari has no skill to fetch"))
    assert da._vendor_skills_block("sayari") is None


def test_fetch_failure_returns_none_so_caller_falls_back_to_plugin(monkeypatch):
    def failing_get(url, **kw):
        raise httpx.ConnectError("github down")

    monkeypatch.setattr(da.httpx, "get", failing_get)
    assert da._vendor_skills_block("cala") is None
