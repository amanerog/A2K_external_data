"""LinkupDefaults (direct_agent.py): maxResults=10 on Linkup's search tool
when the model leaves it out (v5 changelog item 12).

    cd agent && python -m pytest test_direct_agent_linkup_defaults.py -v
"""

from types import SimpleNamespace

import direct_agent as da


def _before(name, tool_input):
    event = SimpleNamespace(tool_use={"toolUseId": "t1", "name": name, "input": tool_input})
    da.LinkupDefaults()._before(event)
    return event.tool_use["input"]


def test_search_without_a_count_gets_ten():
    assert _before("linkup-search", {"query": "q", "depth": "deep"}) == {"query": "q", "depth": "deep", "maxResults": 10}


def test_a_count_the_model_set_is_kept():
    assert _before("linkup-search", {"query": "q", "maxResults": 20})["maxResults"] == 20


def test_other_tools_are_untouched():
    assert _before("linkup-fetch", {"url": "https://x"}) == {"url": "https://x"}


def test_zero_turns_it_off(monkeypatch):
    monkeypatch.setattr(da, "LINKUP_DEFAULT_MAX_RESULTS", 0)
    assert _before("linkup-search", {"query": "q"}) == {"query": "q"}


def test_only_linkup_gets_the_hook():
    assert set(da.VENDOR_HOOKS) == {"linkup"}
