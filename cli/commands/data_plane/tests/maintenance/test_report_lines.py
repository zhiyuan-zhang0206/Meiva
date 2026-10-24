# meiva:note
# What the maintenance stop reports about the pooler's clients and a Postgres escalation. The
# lines are report-only; nothing here changes the stop.

from __future__ import annotations

from cli.commands.data_plane.maintenance.report_lines import (
    escalation_note,
    pooler_clients_line,
    pooler_console_unreadable_line,
)


def _client(n: int) -> str:
    return f"10.1.2.{n}:5432 db=meiva user=meiva_runner state=idle"


def test_remaining_clients_are_counted_and_named() -> None:
    line = pooler_clients_line([_client(7), _client(8)], shown=10)

    assert line == (
        f"2 client(s) still connected when the pooler stops: {_client(7)}; {_client(8)}"
    )


def test_a_long_list_is_cut_with_a_count_of_the_rest() -> None:
    line = pooler_clients_line([_client(n) for n in range(25)], shown=10)

    assert line is not None
    assert line.startswith("25 client(s) still connected when the pooler stops")
    assert line.endswith("; +15 more")
    assert "10.1.2.9:" in line and "10.1.2.10:" not in line


def test_exactly_as_many_clients_as_shown_adds_no_remainder() -> None:
    line = pooler_clients_line([_client(n) for n in range(10)], shown=10)

    assert line is not None and "more" not in line


def test_no_clients_says_nothing() -> None:
    assert pooler_clients_line([], shown=10) is None


def test_an_unreadable_console_is_reported_as_unreadable_not_as_empty() -> None:
    line = pooler_console_unreadable_line("FileNotFoundError('no admin secret')")

    assert line == (
        "pooler clients could not be listed before the pooler stop: "
        "FileNotFoundError('no admin secret')"
    )


def test_an_escalation_note_says_what_was_ended_and_what_was_killed() -> None:
    note = escalation_note("fast shutdown did not complete within 277s", (4242, 4243))

    assert note == (
        "postgres fast shutdown did not complete within 277s; ended by an immediate shutdown "
        "(crash recovery at the next start; unarchived WAL stays in pg_wal), "
        "killed leftover processes: 4242, 4243"
    )


def test_an_escalation_that_killed_nothing_says_none() -> None:
    assert escalation_note("detail", ()).endswith("killed leftover processes: none")
