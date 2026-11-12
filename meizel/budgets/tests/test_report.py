# meiva:note
# The report's mathematical contract is independent of the gate: different owners' units,
# boundary inclusion, malformed measurements and an uncapped complete table.
from __future__ import annotations

import json

import pytest

from meizel.budgets.report import BudgetRow, changed_location, decode_rows, render_table


@pytest.mark.parametrize(
    ("current", "changed", "expected"),
    [
        (719, True, False),
        (720, True, True),
        (800, True, True),
        (801, True, True),
        (799, False, False),
    ],
)
def test_attention_is_changed_and_at_most_ten_percent_remaining(
    current: int,
    changed: bool,
    expected: bool,
) -> None:
    assert BudgetRow("lines", "x.py", current, 800, changed).attention is expected


def test_six_historical_locations_share_one_table_without_a_row_limit() -> None:
    rows = [
        BudgetRow("memory-markdown-files", "agents/rulings", 21, 20, True),
        BudgetRow("directory-entries", "meizel/docs/checker", 20, 20, True),
        BudgetRow("python-lines", "meizel/language/scan.py", 800, 800, True),
        BudgetRow("directory-entries", "scripts/lint/tests", 20, 20, True),
        BudgetRow("note-characters", "scripts/lint/docs/lint.meiva.okf.md", 8187, 8000, True),
        BudgetRow("note-characters", "harness-attach.meiva.okf.md", 7999, 8000, False),
    ]
    table = render_table(rows)
    assert "| 8187 | 8000 | -187 | **yes** |" in table
    assert "| 7999 | 8000 | 1 | no |" in table
    assert all(row.location in table for row in rows)
    assert len(render_table(rows * 100).splitlines()) == 602


@pytest.mark.parametrize(
    "bad",
    [
        None,
        {},
        {"kind": "x"},
        {"kind": "x", "location": "p", "current": True, "limit": 20, "changed": False},
        {"kind": "x", "location": "p", "current": 1, "limit": 0, "changed": True},
        {"kind": "x", "location": "p", "current": 1, "limit": 20, "changed": "yes"},
    ],
)
def test_bad_snapshots_are_rejected_atomically(bad: object) -> None:
    with pytest.raises((ValueError, TypeError)):
        decode_rows(json.dumps({"schema": "meizel.budgets/v1", "rows": [bad]}))


def test_changed_directories_do_not_match_sibling_prefixes() -> None:
    assert changed_location("pkg", ["pkg/removed.py"])
    assert not changed_location("pkg", ["pkg_extra/file.py"])
