"""GatewayEngine._call_agent's token handling: a 401 from the agent Runtime
refetches the Cognito token once and retries (routing run 2026-10-08: a
token cached past its expiry in a frozen microVM gave 8 consecutive 401s)."""

import httpx
import pytest

from a2k.config import config
from a2k.gateway.engine import GatewayEngine


@pytest.fixture
def agent_env(monkeypatch):
    for key, value in {
        "agent_pool_client_id": "id", "agent_pool_client_secret": "secret",
        "agent_pool_token_url": "https://auth.example/token", "agent_runtime_url": "https://agent.example/invocations",
    }.items():
        monkeypatch.setitem(config.__dict__, key, value)


async def test_a_401_refetches_the_token_once_and_retries(monkeypatch, agent_env):
    tokens = iter(["stale", "fresh"])
    sent = []

    async def get_token(self):
        if self._agent_token is None:
            self._agent_token = next(tokens)
        return self._agent_token

    async def post(self, url, json, headers):
        sent.append(headers["Authorization"])
        status = 401 if headers["Authorization"] == "Bearer stale" else 200
        return httpx.Response(status, json={"ok": True, "content": {"answer": "a"}}, request=httpx.Request("POST", url))

    monkeypatch.setattr(GatewayEngine, "_get_agent_token", get_token)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    engine = GatewayEngine()
    engine._agent_token = None
    content = await engine._call_agent("ask", "q", None, "r1")
    assert content == {"answer": "a"} and sent == ["Bearer stale", "Bearer fresh"]


async def test_a_second_401_is_an_upstream_error(monkeypatch, agent_env):
    from a2k.errors import A2KError

    async def get_token(self):
        self._agent_token = "always-rejected"
        return self._agent_token

    async def post(self, url, json, headers):
        return httpx.Response(401, request=httpx.Request("POST", url))

    monkeypatch.setattr(GatewayEngine, "_get_agent_token", get_token)
    monkeypatch.setattr(httpx.AsyncClient, "post", post)
    with pytest.raises(A2KError):
        await GatewayEngine()._call_agent("ask", "q", None, "r1")
