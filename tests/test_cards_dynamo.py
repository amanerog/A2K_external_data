"""Tests for a2k/cards/__init__.py's DynamoDB-backed vendor card loading.

config.vendor_cards_table unset (the default) is already exercised by every
other test in this suite that touches load_card/vendor_catalogue via the
local cala_card.json/sayari_card.json fallback -- this file is specifically
about the DynamoDB path and its TTL cache, mocking boto3 (monkeypatching
boto3.resource) rather than needing a real table.
"""

import json
from types import SimpleNamespace

import boto3
import pytest

import a2k.cards as cards_module


class _FakeTable:
    """One entry in `pages` per expected `.scan()` call, in order."""

    def __init__(self, pages: list[dict]):
        self._pages = pages
        self.scan_calls: list[dict] = []

    def scan(self, **kwargs):
        self.scan_calls.append(kwargs)
        return self._pages[len(self.scan_calls) - 1]


class _FakeDynamoResource:
    def __init__(self, table: _FakeTable):
        self._table = table

    def Table(self, name):
        return self._table


@pytest.fixture(autouse=True)
def _reset_cache(monkeypatch):
    """The module-level TTL cache must not leak between tests -- each test
    below assumes it starts cold."""
    monkeypatch.setattr(cards_module, "_vendor_cards_cache", None)
    yield
    monkeypatch.setattr(cards_module, "_vendor_cards_cache", None)


def _card_item(source_id: str) -> dict:
    """A minimal-but-valid KBCard item, shaped like what a real DynamoDB
    Scan would return -- reuses the real gateway_card.json as a base (any
    valid KBCard works; cards/__init__.py doesn't care which vendor a card
    describes) so this doesn't have to hand-write every required field."""
    with open(cards_module.CARDS_DIR / "gateway_card.json", encoding="utf-8") as fh:
        card = json.load(fh)
    card["id"] = f"urn:a2k:vendor:{source_id}"
    card["name"] = source_id
    return {"sourceId": source_id, "cardJson": json.dumps(card)}


def _use_fake_table(monkeypatch, pages: list[dict], ttl: int = 600) -> _FakeTable:
    fake_table = _FakeTable(pages)
    monkeypatch.setattr(
        cards_module, "config", SimpleNamespace(vendor_cards_table="test-table", vendor_cards_cache_ttl_seconds=ttl)
    )
    monkeypatch.setattr(boto3, "resource", lambda name: _FakeDynamoResource(fake_table))
    return fake_table


def test_vendor_catalogue_reads_from_dynamo_when_table_configured(monkeypatch):
    fake_table = _use_fake_table(monkeypatch, [{"Items": [_card_item("cala"), _card_item("sayari")]}])

    vendors = cards_module.vendor_catalogue()

    assert {v["sourceId"] for v in vendors} == {"cala", "sayari"}
    assert len(fake_table.scan_calls) == 1


def test_vendor_catalogue_picks_up_a_new_vendor_with_no_code_change(monkeypatch):
    """The whole point of this feature: a third vendor that was never
    hardcoded anywhere in this codebase shows up just by existing in the
    (fake) table."""
    _use_fake_table(monkeypatch, [{"Items": [_card_item("cala"), _card_item("sayari"), _card_item("newvendor")]}])

    vendors = cards_module.vendor_catalogue()

    assert "newvendor" in {v["sourceId"] for v in vendors}
    assert cards_module.load_card("newvendor").id == "urn:a2k:vendor:newvendor"


def test_load_card_raises_for_unknown_vendor(monkeypatch):
    _use_fake_table(monkeypatch, [{"Items": [_card_item("cala")]}])

    with pytest.raises(KeyError, match="nonexistent"):
        cards_module.load_card("nonexistent")


def test_dynamo_scan_paginates_via_last_evaluated_key(monkeypatch):
    fake_table = _use_fake_table(
        monkeypatch,
        [
            {"Items": [_card_item("cala")], "LastEvaluatedKey": {"sourceId": "cala"}},
            {"Items": [_card_item("sayari")]},
        ],
    )

    vendors = cards_module.vendor_catalogue()

    assert {v["sourceId"] for v in vendors} == {"cala", "sayari"}
    assert len(fake_table.scan_calls) == 2
    assert fake_table.scan_calls[1] == {"ExclusiveStartKey": {"sourceId": "cala"}}


def test_vendor_cards_are_cached_until_ttl_expires(monkeypatch):
    fake_table = _use_fake_table(monkeypatch, [{"Items": [_card_item("cala")]}])

    cards_module.vendor_catalogue()
    cards_module.vendor_catalogue()
    cards_module.vendor_catalogue()

    assert len(fake_table.scan_calls) == 1  # served from cache after the first fetch


def test_vendor_cards_refetch_after_ttl_expires(monkeypatch):
    fake_table = _use_fake_table(monkeypatch, [{"Items": [_card_item("cala")]}, {"Items": [_card_item("cala")]}])

    cards_module.vendor_catalogue()
    # Force the cached entry to look expired without actually sleeping.
    _, cached = cards_module._vendor_cards_cache
    monkeypatch.setattr(cards_module, "_vendor_cards_cache", (0.0, cached))

    cards_module.vendor_catalogue()

    assert len(fake_table.scan_calls) == 2


def test_falls_back_to_local_files_when_table_unset(monkeypatch):
    monkeypatch.setattr(
        cards_module, "config", SimpleNamespace(vendor_cards_table=None, vendor_cards_cache_ttl_seconds=600)
    )

    vendors = cards_module.vendor_catalogue()

    assert {v["sourceId"] for v in vendors} == {"cala", "sayari"}
