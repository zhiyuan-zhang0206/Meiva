# meiva:note
# The operator path for `schedules.owner_agent_id`: the schedules row and its audit row commit
# together, the audit carries the host that made the assignment and the reason typed, an unchanged
# owner writes nothing, and a blank reason never reaches the database.

from __future__ import annotations

import argparse
from pathlib import Path

import psycopg
import pytest

from base.host.env.boot_policy import PLACEHOLDER_DB_URL
from scripts.host_ops import set_schedule_owner


def _schedule(db_conn: psycopg.Connection, name: str) -> int:
    row = db_conn.execute(
        "INSERT INTO schedules (name, description, script, command, enabled, machine) "
        "VALUES (%s, NULL, 'print(1)', 'python schedule.py', false, 'macmini') RETURNING id",
        (name,),
    ).fetchone()
    assert row is not None
    db_conn.commit()
    return int(row[0])


def _agent(db_conn: psycopg.Connection) -> int:
    from base.db import create_agent

    agent_id = create_agent(db_conn)
    db_conn.commit()
    return agent_id


def test_an_assignment_and_its_reason_are_written_together(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    sid = _schedule(db_conn, "provisioned-schedule")

    previous, outcome = set_schedule_owner.assign_owner(
        db_conn, sid, owner, "the watch speaks for the P0 lead"
    )
    db_conn.commit()

    assert (previous, outcome) == (None, "updated")
    assert db_conn.execute(
        "SELECT owner_agent_id FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (owner,)
    row = db_conn.execute(
        "SELECT old_owner_id, new_owner_id, source, reason, changed_at FROM schedule_owner_changes "
        "WHERE schedule_id = %s",
        (sid,),
    ).fetchone()
    assert row is not None
    assert row[0] is None and row[1] == owner
    assert str(row[2]).startswith("ops:")
    assert row[3] == "the watch speaks for the P0 lead"
    assert row[4] is not None


# meiva:note
# Clearing is an assignment too, and the audit keeps both ends: a reader months later can see who a
# schedule stopped speaking for, not only who it started speaking for.
def test_clearing_records_the_owner_it_removed(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    sid = _schedule(db_conn, "cleared-schedule")
    set_schedule_owner.assign_owner(db_conn, sid, owner, "assigned first")
    db_conn.commit()

    previous, outcome = set_schedule_owner.assign_owner(db_conn, sid, None, "the watch went away")
    db_conn.commit()

    assert (previous, outcome) == (owner, "updated")
    assert db_conn.execute(
        "SELECT owner_agent_id FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (None,)
    assert db_conn.execute(
        "SELECT new_owner_id FROM schedule_owner_changes WHERE schedule_id = %s "
        "ORDER BY id DESC LIMIT 1",
        (sid,),
    ).fetchone() == (None,)


def test_an_unchanged_owner_writes_no_audit_row(db_conn: psycopg.Connection) -> None:
    owner = _agent(db_conn)
    sid = _schedule(db_conn, "steady-schedule")
    set_schedule_owner.assign_owner(db_conn, sid, owner, "assigned once")
    db_conn.commit()

    assert set_schedule_owner.assign_owner(db_conn, sid, owner, "said again") == (
        owner,
        "unchanged",
    )
    db_conn.commit()

    assert db_conn.execute(
        "SELECT count(*) FROM schedule_owner_changes WHERE schedule_id = %s", (sid,)
    ).fetchone() == (1,)


def test_an_unknown_agent_stops_before_the_row_changes(db_conn: psycopg.Connection) -> None:
    sid = _schedule(db_conn, "unowned-schedule")
    with pytest.raises(SystemExit, match="no such agent"):
        set_schedule_owner.assign_owner(db_conn, sid, 999_999, "typo in the agent id")
    db_conn.rollback()
    assert db_conn.execute(
        "SELECT owner_agent_id FROM schedules WHERE id = %s", (sid,)
    ).fetchone() == (None,)


def test_a_blank_reason_is_refused_before_anything_is_written() -> None:
    with pytest.raises(argparse.ArgumentTypeError):
        set_schedule_owner.reason_argument("   ")
    assert set_schedule_owner.reason_argument("  because the card is the P0 lead's  ") == (
        "because the card is the P0 lead's"
    )


# meiva:note
# The trap this tool fell into once: a bare `python scripts/host_ops/...` inherits a shell that
# exported MEIVA_DB_URL, settings ignores that variable, and the run dials the placeholder. The
# refusal has to name where the value should have come from and the variable it ignored, or the
# reader goes looking for a network fault.
#
# The two directions of the fact matter: an absent .env file sends the reader to the home it named,
# a present file without the key sends them into that file. Both are asserted, and both exit
# non-zero rather than printing a warning.
def test_an_absent_env_file_names_the_home(tmp_path: Path) -> None:
    with pytest.raises(SystemExit) as refused:
        set_schedule_owner.connect(PLACEHOLDER_DB_URL, tmp_path)
    message = str(refused.value)
    assert "does not exist" in message
    assert "MEIVA_HOME" in message
    assert "MEIVA_DB_URL" in message
    assert getattr(refused.value, "code", 0) != 0


def test_a_present_env_file_without_the_key_names_the_file(tmp_path: Path) -> None:
    (tmp_path / ".env").write_text("MEIVA_REDIS_URL=redis://127.0.0.1:1/0\n", encoding="utf-8")
    with pytest.raises(SystemExit) as refused:
        set_schedule_owner.connect(PLACEHOLDER_DB_URL, tmp_path)
    message = str(refused.value)
    assert str(tmp_path / ".env") in message
    assert "carries no MEIVA_DB_URL" in message
    assert getattr(refused.value, "code", 0) != 0


# meiva:note
# The other direction: a real URL passes the gate. This asserts the decision, not a dial -- `connect`
# would open a socket, which is the one thing a unit test must not do here.
def test_a_real_url_passes_the_gate(tmp_path: Path) -> None:
    assert (
        set_schedule_owner.unresolvable_reason(
            "postgresql://meiva@db.example:6432/meiva_main", tmp_path
        )
        is None
    )
