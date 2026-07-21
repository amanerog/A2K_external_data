"""The test suite must never depend on the developer's ambient `.env` /
A2K_BOX_MODE -- otherwise `pytest` silently starts hitting real, billed
third-party APIs (and fails hard whenever Sayari isn't live-ready) the
moment someone flips A2K_BOX_MODE=live locally to test Cala, which is
exactly what happened while building this fixture.

`Config` is a frozen dataclass, so its singleton instance can't be
monkeypatched directly; overriding the `is_mock` property on the class
works instead and is restored automatically after each test.

`tests/test_cala_live_adapter.py` is unaffected: it calls the adapter's
private `_live_*` methods directly, which never consult `config.is_mock`.
"""

import pytest

from a2k_box.config import Config


@pytest.fixture(autouse=True)
def _force_mock_mode(monkeypatch):
    monkeypatch.setattr(Config, "is_mock", property(lambda self: True))
