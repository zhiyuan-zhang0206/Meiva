# meiva:note
# The criterion itself is pinned here: both write surfaces (the fleet plugin's update and the
# gateway PATCH) read this module instead of deciding for themselves which write is progress.
# meiva:link base/agents/tasks/reminder_window.py

from __future__ import annotations

import pytest

from base.agents.tasks.reminder_window import (
    DEFAULT_RESET_ON,
    TaskSnapshot,
    UpdateWrite,
    changed_kinds,
    resets_window,
    validate_reset_on,
)

_SNAPSHOT = TaskSnapshot(
    status="in_progress",
    title="chart the estuary",
    description="measure salinity",
    results="sampled twice",
    owner=3,
    priority="P2",
    parent_id=7,
    remind_interval_seconds=86400,
)


def _changed(write: UpdateWrite) -> set[str]:
    return set(changed_kinds(_SNAPSHOT, write))


# meiva:note
# A note is a log line; the ladder must survive it.
def test_note_alone_is_not_a_change_to_the_work() -> None:
    assert _changed(UpdateWrite(note="still blocked")) == {"note"}
    assert not resets_window(frozenset({"note"}), DEFAULT_RESET_ON)


@pytest.mark.parametrize(
    ("write", "kind"),
    [
        (UpdateWrite(status="done"), "status"),
        (UpdateWrite(results="shipped"), "results"),
        (UpdateWrite(owner=9), "owner"),
        (UpdateWrite(priority="P0"), "priority"),
        (UpdateWrite(parent_id=None, parent_written=True), "parent"),
        (UpdateWrite(title="chart the estuary, revised"), "title"),
        (UpdateWrite(description="measure temperature too"), "description"),
        (UpdateWrite(remind_interval_seconds=3600), "remind_interval_seconds"),
    ],
)
def test_a_written_field_that_differs_is_a_change(write: UpdateWrite, kind: str) -> None:
    assert _changed(write) == {kind}


@pytest.mark.parametrize(
    "write",
    [
        UpdateWrite(status="in_progress"),
        UpdateWrite(results="sampled twice"),
        UpdateWrite(owner=3),
        UpdateWrite(priority="P2"),
        UpdateWrite(parent_id=7, parent_written=True),
        UpdateWrite(title="chart the estuary"),
        UpdateWrite(description="measure salinity"),
        UpdateWrite(remind_interval_seconds=86400),
        UpdateWrite(parent_id=None),
    ],
)
# meiva:note
# Agents restate status and results routinely; a restatement is not progress.
def test_restating_the_current_value_is_not_a_change(write: UpdateWrite) -> None:
    assert _changed(write) == set()


@pytest.mark.parametrize("value", ["", "   ", "\n"])
# meiva:note
# Clearing a field says nothing about the work.
def test_empty_text_never_counts_as_a_change(value: str) -> None:
    assert _changed(UpdateWrite(results=value)) == set()
    assert _changed(UpdateWrite(description=value)) == set()


def test_default_policy_names_the_work_not_the_record_of_it() -> None:
    assert set(DEFAULT_RESET_ON) == {"status", "results", "owner", "priority", "parent"}
    assert not resets_window(frozenset({"note", "title"}), DEFAULT_RESET_ON)
    assert resets_window(frozenset({"note", "status"}), DEFAULT_RESET_ON)


def test_policy_rejects_an_unknown_kind() -> None:
    with pytest.raises(ValueError, match="unknown update kinds"):
        validate_reset_on(("status", "mood"))


# meiva:note
# Nothing would ever re-arm `escalated_at`, so the task could escalate once and stay escalated.
def test_policy_rejects_an_empty_list() -> None:
    with pytest.raises(ValueError, match="at least one kind"):
        validate_reset_on(())
