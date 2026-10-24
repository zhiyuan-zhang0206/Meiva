# meiva:note
# The reconcile tool's decisions are values in, text or a verdict out.

from __future__ import annotations

import pytest

from services.derived.memory_indexer.reconcile_plan import (
    WriteConfirmation,
    confirm_write,
    connection_refusal_lines,
    ordering_lines,
    refusal_message,
    row_gap_lines,
    summary_line,
)


@pytest.mark.parametrize(
    ("interactive", "answer", "expected"),
    [
        (True, "yes", "confirmed"),
        (True, "yes ", "not_exact"),
        (True, "YES", "not_exact"),
        (True, "", "not_exact"),
        (True, None, "eof"),
        (False, "yes", "no_terminal"),
        (False, None, "no_terminal"),
    ],
)
def test_only_the_exact_word_on_a_terminal_confirms_a_write(
    interactive: bool, answer: str | None, expected: WriteConfirmation
) -> None:
    assert confirm_write(interactive=interactive, answer=answer) == expected


def test_each_refused_confirmation_has_its_own_message() -> None:
    assert refusal_message("confirmed") is None
    messages = {
        verdict: refusal_message(verdict) for verdict in ("no_terminal", "eof", "not_exact")
    }
    assert messages == {
        "no_terminal": "--allow-write requires an interactive terminal; no backend connected",
        "eof": "--allow-write confirmation was unavailable (EOF); no backend connected",
        "not_exact": "--allow-write was not confirmed; no backend connected",
    }


def test_a_read_only_refusal_points_at_the_next_patrol() -> None:
    lines = connection_refusal_lines(RuntimeError("table schema mismatch"), readonly=True)
    assert lines[0] == "read-only backend connection refused: table schema mismatch"
    assert "next patrol (15 minutes by default) repairs the backend" in lines[1]
    assert len(lines) == 2


def test_a_writable_failure_is_only_reported() -> None:
    assert connection_refusal_lines(ValueError("numpy unavailable"), readonly=False) == (
        "backend connection failed: numpy unavailable",
    )


def test_equal_row_sets_have_no_gap() -> None:
    assert row_gap_lines("a", "b", {"/x.md": 1, "/y.md": 2}, ["/y.md", "/x.md"]) == ()


def test_a_row_set_gap_names_the_paths_missing_on_each_side() -> None:
    lines = row_gap_lines("numpy", "pgvector", ["/x.md", "/only-a.md"], ["/x.md", "/only-b.md"])
    assert lines == (
        "row sets differ: numpy=2 paths, pgvector=2; only in numpy: ['/only-a.md']",
        "only in pgvector: ['/only-b.md']",
        "sync the backends with the indexer daemon's next patrol and re-run",
    )


def test_a_row_set_gap_lists_at_most_five_paths_per_side() -> None:
    lines = row_gap_lines("a", "b", [f"/{i}.md" for i in range(9)], [])
    assert lines[0].endswith("only in a: ['/0.md', '/1.md', '/2.md', '/3.md', '/4.md']")


def test_identical_orderings_say_nothing() -> None:
    assert ordering_lines(1, "query", "a", "b", ["/x.md", "/y.md"], ["/x.md", "/y.md"]) == ()


def test_differing_orderings_show_both_sides() -> None:
    assert ordering_lines(3, "q" * 80, "numpy", "pgvector", ["/x.md", "/y.md"], ["/y.md"]) == (
        f"[3] {'q' * 60!r}",
        "         numpy: ['/x.md', '/y.md']",
        "      pgvector: ['/y.md']",
    )


def test_the_summary_counts_identical_queries() -> None:
    assert summary_line(4, 5, "numpy", "pgvector", 10) == (
        "summary: 4/5 queries identical (numpy vs pgvector, k=10)"
    )
