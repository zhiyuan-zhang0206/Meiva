from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from base.agents.epochs.model import HarnessEpoch
from base.agents.history.timeline_item import TimelineItem
from gateway.agents.history.epoch_pages import (
    EpochRead,
    Unavailable,
    assemble_page,
    page_target,
    switch_block,
    usable_epochs,
)
from gateway.agents.history.epoch_range import external_range
from gateway.message_admission.cursor import parse_cursor

_T0 = datetime(2026, 11, 25, 8, 0, tzinfo=UTC)


def _epoch(
    seq: int,
    harness: str,
    *,
    model: str | None = None,
    reason: str | None = None,
    start: int | None = None,
    end: int | None = None,
    ended: bool = True,
) -> HarnessEpoch:
    return HarnessEpoch(
        agent_id=1,
        seq=seq,
        harness=harness,
        model=model,
        session_id=None,
        started_at=_T0 + timedelta(hours=seq),
        ended_at=_T0 + timedelta(hours=seq + 1) if ended else None,
        source_start=start,
        source_end=end,
        switched_by="birth" if seq == 0 else "user",
        reason=reason,
    )


def _chat(item_id: str) -> TimelineItem:
    return TimelineItem(item_id=item_id, kind="agent_chat", payload="x")


_CHAIN = [
    _epoch(0, "meiva"),
    _epoch(1, "claude_code", model="claude-sonnet-5-5", reason="handoff in /tmp/h.md"),
    _epoch(2, "codex"),
    _epoch(3, "meiva", ended=False),
]


def test_a_chain_of_epochs_is_usable_when_it_ends_on_the_agents_harness() -> None:
    assert usable_epochs(_CHAIN, "meiva") == _CHAIN
    assert usable_epochs(_CHAIN, "codex") == []
    assert usable_epochs(_CHAIN[:1], "meiva") == []
    assert usable_epochs([], "meiva") == []
    assert usable_epochs([_CHAIN[0], _CHAIN[2], _CHAIN[3]], "meiva") == []
    assert usable_epochs(_CHAIN[:3], "codex") == []


def test_the_switch_block_names_both_harnesses_the_model_and_the_restart_message() -> None:
    block = switch_block(_CHAIN[1], _CHAIN[0])
    assert (block.item_id, block.kind, block.source, block.show_timestamp) == (
        "e1.sw",
        "system_marker",
        "harness_switch",
        True,
    )
    assert block.payload == (
        "Switched harness: meiva -> claude_code (claude-sonnet-5-5)\n\n"
        "Restart message: handoff in /tmp/h.md"
    )
    assert block.created_at == _CHAIN[1].started_at.isoformat()
    assert switch_block(_CHAIN[2], _CHAIN[1]).payload == "Switched harness: claude_code -> codex"


@pytest.mark.parametrize(
    ("before", "seq", "local"),
    [
        (None, 3, None),
        ("40.0", 3, "40.0"),
        ("e1.h900.0", 1, "h900.0"),
        ("e0.s1.cp.3.0", 0, "3.0"),
        ("e3.sw", 2, None),
        ("e1.sw", 0, None),
    ],
)
def test_a_cursor_selects_the_epoch_it_pages(
    before: str | None, seq: int, local: str | None
) -> None:
    cursor = None if before is None else parse_cursor(before)
    target = page_target(_CHAIN, before, cursor)
    assert (target.seq, None if target.within is None else target.within.item_id()) == (seq, local)
    assert target.fixed is None


@pytest.mark.parametrize("before", ["e0.sw", "e0.unavailable", "e9.sw", "e9.1.0", "garbage"])
def test_a_cursor_with_nothing_older_or_no_such_epoch_is_an_empty_final_page(before: str) -> None:
    target = page_target(_CHAIN, before, parse_cursor(before))
    assert (target.seq, target.fixed) == (None, [])


def test_the_current_epoch_keeps_unprefixed_ids_and_ends_with_its_switch_block() -> None:
    page = assemble_page(_CHAIN, 3, EpochRead([_chat("40.0")], has_more=False, msg_count=44))
    assert [item.item_id for item in page.items] == ["e3.sw", "40.0"]
    assert (page.has_more, page.msg_count) == (True, 44)


def test_a_window_inside_an_epoch_has_no_switch_block_and_more_to_read() -> None:
    page = assemble_page(_CHAIN, 2, EpochRead([_chat("h9.0")], has_more=True, msg_count=77))
    assert [item.item_id for item in page.items] == ["e2.h9.0"]
    assert (page.has_more, page.msg_count) == (True, 0)


def test_the_first_epoch_has_no_switch_block_and_nothing_older() -> None:
    page = assemble_page(_CHAIN, 0, EpochRead([_chat("0.0")], has_more=False))
    assert [item.item_id for item in page.items] == ["e0.0.0"]
    assert page.has_more is False


def test_an_unreadable_epoch_is_only_its_switch_block_and_the_first_one_is_nothing() -> None:
    read = EpochRead([], has_more=False, unavailable=Unavailable.FILE_MISSING)
    page = assemble_page(_CHAIN, 1, read)
    assert [item.item_id for item in page.items] == ["e1.sw"]
    assert page.has_more is True
    first = assemble_page(_CHAIN, 0, read)
    assert (first.items, first.has_more) == ([], False)


def test_an_epoch_that_reports_more_but_returns_nothing_is_treated_as_read_to_its_head() -> None:
    page = assemble_page(_CHAIN, 2, EpochRead([], has_more=True))
    assert [item.item_id for item in page.items] == ["e2.sw"]
    assert page.has_more is True


def test_external_range_uses_the_recorded_offsets() -> None:
    chain = [
        _epoch(0, "claude_code", start=0, end=500),
        _epoch(1, "codex", start=0, end=90),
        _epoch(2, "claude_code", start=500, ended=False),
    ]
    assert (external_range(chain, chain[0]).floor, external_range(chain, chain[0]).ceiling) == (
        0,
        500,
    )
    assert (external_range(chain, chain[2]).floor, external_range(chain, chain[2]).ceiling) == (
        500,
        None,
    )


def test_external_range_infers_a_bound_from_a_sibling_epoch_of_the_same_harness() -> None:
    chain = [
        _epoch(0, "claude_code"),
        _epoch(1, "codex"),
        _epoch(2, "claude_code", start=500, ended=False),
    ]
    first = external_range(chain, chain[0])
    assert (first.floor, first.ceiling, first.timed) == (None, 500, False)


def test_external_range_falls_back_to_time_when_siblings_have_no_offsets() -> None:
    chain = [_epoch(0, "claude_code"), _epoch(1, "codex"), _epoch(2, "claude_code", ended=False)]
    span = external_range(chain, chain[0])
    assert (span.floor, span.ceiling, span.timed) == (None, None, True)
    assert (span.since, span.until) == (chain[0].started_at, chain[0].ended_at)
    alone = external_range(chain, chain[1])
    assert (alone.floor, alone.ceiling, alone.timed) == (None, None, False)
