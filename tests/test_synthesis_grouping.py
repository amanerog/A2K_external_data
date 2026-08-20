"""Unit tests for `group_facts()`'s conflict-grouping join key -- in
particular the `multi_valued` exception added 2026-08-20 after a real false
positive: Cala relationship facts (e.g. `IS_SUBSIDIARY_OF`) naturally have
many simultaneously-true values for the same field, which the plain
(entity_key, field) join key flagged as "disputed" just because there was
more than one.
"""

from a2k.adapters.base import Fact
from a2k.gateway.synthesis import group_facts


def _fact(*, field: str, value: str, kb_id: str = "urn:a2k:vendor:cala", multi_valued: bool = False) -> Fact:
    return Fact(
        entity_key="microsoft",
        entity_name="Microsoft Corp",
        field=field,
        value=value,
        text=f"Microsoft Corp {field} {value}.",
        document_id="doc-1",
        title="Cala",
        source_url=None,
        source_hash="sha256:x",
        retrieved_at="2026-08-20T00:00:00Z",
        source_last_updated=None,
        kb_id=kb_id,
        multi_valued=multi_valued,
    )


def test_single_valued_field_with_two_values_is_still_disputed():
    facts = [
        _fact(field="employee_count", value="228000"),
        _fact(field="employee_count", value="250000", kb_id="urn:a2k:vendor:sayari"),
    ]
    groups = group_facts(facts)
    assert len(groups) == 1
    assert groups[0].disputed is True


def test_multi_valued_field_with_many_values_is_not_disputed():
    facts = [
        _fact(field="IS_SUBSIDIARY_OF", value="LinkedIn Corp", multi_valued=True),
        _fact(field="IS_SUBSIDIARY_OF", value="Activision Blizzard", multi_valued=True),
        _fact(field="IS_SUBSIDIARY_OF", value="Nuance Communications", multi_valued=True),
    ]
    groups = group_facts(facts)
    # three distinct values -> three separate, each non-disputed, groups --
    # not one group with three "conflicting" values.
    assert len(groups) == 3
    assert all(not g.disputed for g in groups)


def test_multi_valued_field_same_value_from_two_sources_still_corroborates():
    facts = [
        _fact(field="IS_SUBSIDIARY_OF", value="LinkedIn Corp", multi_valued=True, kb_id="urn:a2k:vendor:cala"),
        _fact(field="IS_SUBSIDIARY_OF", value="LinkedIn Corp", multi_valued=True, kb_id="urn:a2k:vendor:sayari"),
    ]
    groups = group_facts(facts)
    assert len(groups) == 1
    assert groups[0].disputed is False
    assert len(groups[0].facts_by_value["LinkedIn Corp"]) == 2


def test_multi_valued_field_different_values_across_sources_is_a_known_blind_spot():
    """Documents a deliberate, accepted trade-off (see Fact.multi_valued's
    docstring) -- NOT a case this design catches. If Cala and Sayari report
    genuinely different values for what should be a single-answer
    relationship (e.g. entity X's one true direct parent), they land in two
    separate, non-disputed groups: real disagreement, silently missed. Only
    identical-value reports from multiple sources still corroborate (see
    test above). Accepted 2026-08-20 to kill an actually-occurring false
    positive (legitimate one-to-many relationship values, e.g. many
    subsidiaries, being flagged as "disputed"), at the cost of this
    narrower true-negative on multi_valued fields specifically.
    """
    facts = [
        _fact(field="IS_DIRECT_PARENT_OF_BY", value="Holding A", multi_valued=True, kb_id="urn:a2k:vendor:cala"),
        _fact(field="IS_DIRECT_PARENT_OF_BY", value="Holding B", multi_valued=True, kb_id="urn:a2k:vendor:sayari"),
    ]
    groups = group_facts(facts)
    assert len(groups) == 2
    assert all(not g.disputed for g in groups)
