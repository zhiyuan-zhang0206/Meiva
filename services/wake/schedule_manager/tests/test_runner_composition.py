# meiva:note
# The runner entry composes one process: bind the schedule's actor, open its history, run its script.
#
# These run the production entry (`runner.run`) on a real schedule row; a schedule script is the
# probe, because it runs inside the composed process after everything the entry sets up.

import threading
from pathlib import Path

import psycopg
import pytest

from services.wake.schedule_manager import runner


def _schedule(conn: psycopg.Connection, script: str = "pass") -> int:
    row = conn.execute(
        "INSERT INTO schedules (name, script, command) "
        "VALUES ('composition', %s, 'python schedule.py') RETURNING id",
        (script,),
    ).fetchone()
    conn.commit()
    assert row is not None
    return row[0]


def test_the_script_runs_as_the_schedule_with_its_history_open_under_the_guard(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    marker = unit_home / "world.txt"
    script = (
        # meiva:note
        # a probe script that queries, not SQL built from input.
        "import threading\n"  # noqa: S608
        "from pathlib import Path\n"
        "import meiva\n"
        "from meiva.sdk_surface.agent_identity import require_actor\n"
        "from schedules.entry import schedule_entry\n"
        "with schedule_entry(MEIVA_SCHEDULE_INPUTS) as inputs:\n"
        "    with inputs.database().connect() as conn:\n"
        "        open_runs = conn.execute('SELECT ok FROM schedule_runs').fetchall()\n"
        "    guards = [t.name for t in threading.enumerate() if t.name.endswith('-stall-guard')]\n"
        f"    Path({str(marker)!r}).write_text(\n"
        "        repr((require_actor(), meiva.context.require_clock().timezone, open_runs, guards))\n"
        "    )\n"
    )
    sid = _schedule(db_conn, script)

    assert runner.run(sid) == 0

    # meiva:note
    # the test's own repr.
    actor, timezone, open_runs, guards = eval(marker.read_text())  # noqa: S307
    assert actor == f"schedule:{sid}"
    assert timezone
    assert open_runs == [(None,)]
    assert guards == [f"schedule-{sid}-stall-guard"]
    assert db_conn.execute("SELECT ok FROM schedule_runs").fetchall() == [(True,)]
    assert not [t for t in threading.enumerate() if t.name.endswith("-stall-guard")]


def test_a_script_crash_closes_its_history_and_stops_the_guard(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    sid = _schedule(db_conn, "raise RuntimeError('boom')\n")

    assert runner.run(sid) == 1

    assert db_conn.execute("SELECT ok FROM schedule_runs").fetchall() == [(False,)]
    assert not [t for t in threading.enumerate() if t.name.endswith("-stall-guard")]


def test_main_refuses_on_foreign_checkout(unit_home: Path) -> None:
    (unit_home / "source").mkdir()
    with pytest.raises(SystemExit) as caught:
        runner.main(["1"])
    assert caught.value.code == 3


def test_runpy_script_receives_the_same_entry_builders(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    sid = _schedule(db_conn)
    marker = unit_home / "borrowed.txt"
    script = (
        "import meiva\n"
        "from pathlib import Path\n"
        "from meiva.sdk_surface import process_context\n"
        "from schedules.entry import schedule_entry\n"
        "with schedule_entry(MEIVA_SCHEDULE_INPUTS) as inputs:\n"
        "    assert inputs is MEIVA_SCHEDULE_INPUTS\n"
        "    assert inputs.producer == meiva.context.clients.event_pipeline\n"
        "    assert inputs.image is process_context.loaded_code_image()\n"
        "    assert inputs.machine is meiva.context.require_machine()\n"
        f"    Path({str(marker)!r}).write_text('borrowed')\n"
    )
    db_conn.execute("UPDATE schedules SET script = %s WHERE id = %s", (script, sid))
    db_conn.commit()

    assert runner.run(sid) == 0
    assert marker.read_text() == "borrowed"
