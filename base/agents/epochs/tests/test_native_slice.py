from __future__ import annotations

from datetime import UTC, datetime, timedelta

from base.agents.epochs.model import HarnessEpoch
from base.agents.epochs.native_slice import apply_native_slice, native_slice_of
from base.agents.history.timeline_item import TimelineItem

_T0 = datetime(2026, 11, 25, 8, 0, tzinfo=UTC)


def _epoch(seq: int, harness: str, start: int, end: int | None) -> HarnessEpoch:
    return HarnessEpoch(
        agent_id=1,
        seq=seq,
        harness=harness,
        model=None,
        session_id=None,
        started_at=_T0 + timedelta(hours=start),
        ended_at=None if end is None else _T0 + timedelta(hours=end),
        source_start=None,
        source_end=None,
        switched_by="birth" if seq == 0 else "user",
        reason=None,
    )


def _item(item_id: str, hour: int | None) -> TimelineItem:
    return TimelineItem(
        item_id=item_id,
        kind="agent_chat",
        payload=item_id,
        created_at=None if hour is None else (_T0 + timedelta(hours=hour, minutes=30)).isoformat(),
    )


_EPOCHS = [
    _epoch(0, "meiva", 0, 1),
    _epoch(1, "claude_code", 1, 2),
    _epoch(2, "meiva", 2, None),
]


def _ids(epoch_seq: int, items: list[TimelineItem]) -> list[str]:
    epoch_slice = native_slice_of(_EPOCHS, epoch_seq)
    assert epoch_slice is not None
    return [item.item_id for item in apply_native_slice(items, epoch_slice)]


def test_an_agent_with_one_native_stretch_gets_no_slice() -> None:
    assert native_slice_of(_EPOCHS[:2], 0) is None


def test_each_native_stretch_holds_the_items_of_its_own_time() -> None:
    items = [_item("0.0", None), _item("1.0", 0), _item("2.0", 0), _item("3.0", 2), _item("4.0", 3)]
    assert _ids(0, items) == ["0.0", "1.0", "2.0"]
    assert _ids(2, items) == ["3.0", "4.0"]


def test_an_item_without_a_time_follows_the_item_before_it() -> None:
    items = [_item("0.0", 0), _item("1.0", None), _item("2.0", 2), _item("3.0", None)]
    assert _ids(0, items) == ["0.0", "1.0"]
    assert _ids(2, items) == ["2.0", "3.0"]


def test_a_leading_run_without_a_time_does_not_move_into_a_later_epoch() -> None:
    items = [_item("0.0", None), _item("1.0", None), _item("2.0", 2)]
    assert _ids(0, items) == ["0.0", "1.0"]
    assert _ids(2, items) == ["2.0"]


def test_a_segment_without_any_time_belongs_to_the_first_native_stretch() -> None:
    bare = [_item("0.0", None), _item("1.0", None)]
    assert _ids(0, bare) == ["0.0", "1.0"]
    assert _ids(2, bare) == []


def test_an_instant_on_the_boundary_belongs_to_the_stretch_it_starts() -> None:
    boundary = TimelineItem(
        item_id="1.0", kind="agent_chat", payload="x", created_at=_EPOCHS[2].started_at.isoformat()
    )
    assert _ids(0, [boundary]) == []
    assert _ids(2, [boundary]) == ["1.0"]
