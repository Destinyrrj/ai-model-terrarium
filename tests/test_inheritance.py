from __future__ import annotations

import pytest
from pydantic import ValidationError

from terrarium.inheritance import (
    InheritanceManager,
    Legacy,
    LegacyChannel,
    TiktokenCodec,
)


def test_legacy_is_token_truncated_and_immutable() -> None:
    manager = InheritanceManager(TiktokenCodec())
    legacy = manager.create(
        author="A1",
        generation=0,
        valley="valley_A",
        channel=LegacyChannel.WRITTEN,
        text="red berries after rain are poisonous " * 100,
        parent_legacy_ids=(),
        max_tokens=12,
    )
    assert manager.codec.count(legacy.text) <= 12
    with pytest.raises(ValidationError, match="frozen"):
        legacy.text = "rewritten"  # type: ignore[misc]


def test_provenance_and_deterministic_identity() -> None:
    manager = InheritanceManager(TiktokenCodec())
    parent = manager.create(
        author="A1",
        generation=0,
        valley="valley_A",
        channel=LegacyChannel.WRITTEN,
        text="Never eat red berries after rain.",
        parent_legacy_ids=(),
        max_tokens=500,
    )
    child = manager.create(
        author="A2",
        generation=1,
        valley="valley_A",
        channel=LegacyChannel.WRITTEN,
        text=parent.text,
        parent_legacy_ids=(parent.id,),
        max_tokens=500,
    )
    assert child.parent_legacy_ids == (parent.id,)
    same = manager.create(
        author="A2",
        generation=1,
        valley="valley_A",
        channel=LegacyChannel.WRITTEN,
        text=parent.text,
        parent_legacy_ids=(parent.id,),
        max_tokens=500,
    )
    assert same.id == child.id


def test_parent_selection_uses_supplied_world_rng() -> None:
    manager = InheritanceManager(TiktokenCodec())
    records: list[Legacy] = []
    for index in range(4):
        records.append(
            manager.create(
                author=f"A{index}",
                generation=0,
                valley="valley_A",
                channel=LegacyChannel.WRITTEN,
                text=f"record {index}",
                parent_legacy_ids=(),
                max_tokens=100,
            )
        )
    draws: list[int] = []

    def choose(size: int) -> int:
        draws.append(size)
        return size - 1

    selected = manager.select_parent_ids(
        preferred_ids=(records[0].id,),
        candidate_ids=(record.id for record in records),
        count=3,
        draw_index=choose,
    )
    assert selected[0] == records[0].id
    assert draws == [3]
    assert len(set(selected)) == 2


def test_mature_lineage_always_reserves_one_foreign_record() -> None:
    manager = InheritanceManager(TiktokenCodec())
    records = [
        manager.create(
            author=f"A{index}",
            generation=index,
            valley="valley_A",
            channel=LegacyChannel.WRITTEN,
            text=f"record {index}",
            parent_legacy_ids=(),
            max_tokens=100,
        )
        for index in range(4)
    ]
    draws: list[int] = []
    selected = manager.select_parent_ids(
        preferred_ids=(records[0].id, records[1].id, records[2].id),
        candidate_ids=(record.id for record in records),
        count=3,
        draw_index=lambda upper: draws.append(upper) or 0,
    )

    assert selected[:2] == (records[2].id, records[1].id)
    assert selected[2] == records[3].id
    assert draws == [1]


def test_unknown_parent_is_rejected() -> None:
    manager = InheritanceManager(TiktokenCodec())
    with pytest.raises(ValueError, match="unknown parent"):
        manager.create(
            author="A1",
            generation=1,
            valley="valley_A",
            channel=LegacyChannel.WRITTEN,
            text="claim",
            parent_legacy_ids=("leg_" + "0" * 24,),
            max_tokens=10,
        )
