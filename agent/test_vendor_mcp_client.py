"""Unit test for vendor_mcp_client.py's connect_vendor() dispatch -- the
one piece of that module that's pure logic (connect_cala()/connect_sayari()
themselves need a real network round trip, not unit-testable here). Run
from within this directory, same as test_direct_agent_merge.py:

    cd agent && python -m pytest test_vendor_mcp_client.py -v
"""

import pytest

import vendor_mcp_client as vmc


def test_connect_vendor_dispatches_by_source_id(monkeypatch):
    monkeypatch.setattr(vmc, "connect_cala", lambda: ("cala-client", ["cala-tool"]))
    monkeypatch.setattr(vmc, "connect_sayari", lambda: ("sayari-client", ["sayari-tool"]))
    # VENDOR_CONNECTORS captured the original function objects at import time,
    # so it needs re-pointing too -- same reasoning direct_agent.py's own
    # module-level dispatch dict would need if it were monkeypatched this way.
    monkeypatch.setitem(vmc.VENDOR_CONNECTORS, "cala", vmc.connect_cala)
    monkeypatch.setitem(vmc.VENDOR_CONNECTORS, "sayari", vmc.connect_sayari)

    assert vmc.connect_vendor("cala") == ("cala-client", ["cala-tool"])
    assert vmc.connect_vendor("sayari") == ("sayari-client", ["sayari-tool"])


def test_connect_vendor_unknown_source_id_raises():
    with pytest.raises(ValueError, match="unknown-vendor"):
        vmc.connect_vendor("unknown-vendor")
