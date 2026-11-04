# meiva:note
# The judgement rules, each handed a value: no file, no clock, no git. These are the cases that
# used to be reachable only by running the whole tool against a real volume.
# meiva:link scripts/host_ops/docs/worktree-gc.meiva.okf.md

from __future__ import annotations

import pytest

from scripts.host_ops.worktree_gc.decide import cooloff_block, on_never_list


@pytest.mark.parametrize(
    ("merged_at", "merged_age_min", "commit_age_min", "expected"),
    [
        (None, None, 600.0, None),
        ("2026-11-05T00:00:00Z", 10.0, 600.0, "merge-cooloff"),
        ("2026-11-05T00:00:00Z", 45.0, 600.0, None),
        ("", None, 600.0, "merge-time-unknown"),
        ("2026-11-05T00:00:00Z", 45.0, 5.0, "commit-cooloff"),
        ("2026-11-05T00:00:00Z", 45.0, None, "commit-age-unknown"),
    ],
)
def test_cooloff_names_the_clock_that_is_still_running(
    merged_at, merged_age_min, commit_age_min, expected
) -> None:
    blocked = cooloff_block(merged_at, merged_age_min, commit_age_min, 30.0, 60.0)
    assert (blocked[0] if blocked else None) == expected


def test_cooloff_reports_the_reading_it_used() -> None:
    code, detail = cooloff_block("2026-11-05T00:00:00Z", 12.0, 600.0, 30.0, 60.0)
    assert code == "merge-cooloff"
    assert "12 min" in detail


def test_never_list_matches_the_directory_name_case_insensitively() -> None:
    assert on_never_list("Signal-State", ["signal-*"])
    assert not on_never_list("signal-state", ["other-*"])


def test_never_list_does_not_match_a_path() -> None:
    # meiva:note
    # A pattern is matched against the directory name alone, so `/` in a pattern cannot make one
    # entry silence a whole root.
    assert not on_never_list("signal-state", ["*/signal-*"])
