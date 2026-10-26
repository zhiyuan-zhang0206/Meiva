# meiva:note
# A built-in schedule script as the runner runs it.
#
# The script is stored in the database and materialized into the schedule's own directory, away
# from the checkout, so its imports must resolve from the installed module roots alone
# (`meiva schedules verify` checks the same for the stored copies). Run as `__main__` with a
# schedule id and a database, each script assembles its schedule and reaches its loop: a child
# process that is still waiting a few seconds after it started has got past every line of
# assembly, which is where a wrong keyword or a misspelled name would show.

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

import psycopg

from base.host.proc import run_bounded
from schedules.tests.schedule_support import insert_schedule

_SCHEDULES = Path(__file__).resolve().parents[1]
_CHILD = Path(__file__).parent / "children" / "run_scripts.py"
_SCRIPTS = sorted(_SCHEDULES.glob("*-schedule.py"))


def test_every_builtin_script_imports_from_a_materialized_copy(tmp_path: Path) -> None:
    copies: list[Path] = []
    for index, script in enumerate(_SCRIPTS, start=11):
        directory = tmp_path / "schedules" / str(index)
        directory.mkdir(parents=True)
        copies.append(Path(shutil.copy(script, directory / script.name)))
    assert len(copies) == 11

    child = run_bounded(
        [sys.executable, "-I", str(_CHILD), "imports", *map(str, copies)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=120,
    )

    assert child.returncode == 0, child.stderr
    outcome = json.loads(child.stdout)
    assert {Path(path).name: result for path, result in outcome.items()} == {
        copy.name: "ok" for copy in copies
    }


def test_every_script_run_as_main_reaches_its_loop(
    db_conn: psycopg.Connection, tmp_path: Path
) -> None:
    created_at = datetime.now(UTC)
    workers: list[tuple[str, subprocess.Popen[bytes]]] = []
    for script in _SCRIPTS:
        schedule_id = insert_schedule(db_conn, name=f"entry-{script.name}", created_at=created_at)
        workers.append(
            (
                script.name,
                subprocess.Popen(
                    [sys.executable, "-I", str(_CHILD), "main", str(script)],
                    cwd=tmp_path,
                    env={**os.environ, "MEIVA_SCHEDULE_ID": str(schedule_id)},
                    stderr=subprocess.PIPE,
                ),
            )
        )
    try:
        deadline = time.monotonic() + 10
        while all(worker.poll() is None for _, worker in workers) and time.monotonic() < deadline:
            time.sleep(0.1)
        exited = {name: worker for name, worker in workers if worker.poll() is not None}
        errors = {
            name: (worker.stderr.read().decode()[-600:] if worker.stderr else "")
            for name, worker in exited.items()
        }
    finally:
        for _, worker in workers:
            worker.kill()
        for _, worker in workers:
            worker.wait(timeout=10)

    assert errors == {}
