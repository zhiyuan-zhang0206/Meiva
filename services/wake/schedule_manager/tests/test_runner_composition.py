# meiva:note
# The runner entry composes one process: bind the schedule's actor, open its history, run its script.
#
# These run the production entry (`runner.run`) on a real schedule row; a schedule script is the
# probe, because it runs inside the composed process after everything the entry sets up.

import threading
from pathlib import Path

import psycopg
import pytest

from base.agents.context.clients import ClientSet
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.config.tests.configuration import data_plane_environment
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database, create_agent
from base.db.code_version_gate import ProcessDbGate
from base.host.exit_codes import hard_exit
from base.native_process.code_version import CodeVersion
from base.paths import meiva_home
from base.telemetry import process_name
from meiva.boot import clients_over
from meiva.sdk_surface import process_context
from services.wake.schedule_manager import engine, runner
from services.wake.schedule_manager.stall_policy import StallPolicy


# meiva:note
# `runner.run` defaults to a fresh `ConfigBoot()` over `os.environ`, which holds the
# unreachable sentinel unless a test hands the runner the provisioned data plane.
def _runner_boot() -> ConfigBoot:
    config = ConfigBoot(environment=data_plane_environment())
    config.read_process_environment()
    return config


def _schedule(conn: psycopg.Connection, script: str = "pass") -> int:
    row = conn.execute(
        "INSERT INTO schedules (name, script, command, machine) "
        "VALUES ('composition', %s, 'python schedule.py', 'gateway') RETURNING id",
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
        "from schedules.jobs.entry import schedule_entry\n"
        "with schedule_entry(MEIVA_SCHEDULE_INPUTS) as inputs:\n"
        "    with inputs.database().connect() as conn:\n"
        "        open_runs = conn.execute('SELECT ok FROM schedule_runs').fetchall()\n"
        "    guards = [t.name for t in threading.enumerate() if t.name.endswith('-stall-guard')]\n"
        f"    Path({str(marker)!r}).write_text(\n"
        "        repr((require_actor(), meiva.context.require_clock().timezone, open_runs, guards,\n"
        "              type(meiva.context.require_bus()).__name__))\n"
        "    )\n"
    )
    sid = _schedule(db_conn, script)

    assert runner.run(sid, config=_runner_boot()) == 0

    # meiva:note
    # the test's own repr.
    actor, timezone, open_runs, guards, bus_type = eval(marker.read_text())  # noqa: S307
    assert actor == f"schedule:{sid}"
    assert timezone
    assert open_runs == [(None,)]
    assert guards == [f"schedule-{sid}-stall-guard"]
    assert bus_type == "EventBus"
    assert db_conn.execute("SELECT ok FROM schedule_runs").fetchall() == [(True,)]
    assert not [t for t in threading.enumerate() if t.name.endswith("-stall-guard")]


def test_a_script_crash_closes_its_history_and_stops_the_guard(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    sid = _schedule(db_conn, "raise RuntimeError('boom')\n")

    assert runner.run(sid, config=_runner_boot()) == 1

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
        "from schedules.jobs.entry import schedule_entry\n"
        "with schedule_entry(MEIVA_SCHEDULE_INPUTS) as inputs:\n"
        "    assert inputs is MEIVA_SCHEDULE_INPUTS\n"
        "    assert inputs.producer == meiva.context.clients.event_pipeline\n"
        "    assert inputs.image is process_context.loaded_code_image()\n"
        "    assert inputs.machine is meiva.context.require_machine()\n"
        f"    Path({str(marker)!r}).write_text('borrowed')\n"
    )
    db_conn.execute("UPDATE schedules SET script = %s WHERE id = %s", (script, sid))
    db_conn.commit()

    assert runner.run(sid, config=_runner_boot()) == 0
    assert marker.read_text() == "borrowed"


# meiva:note
# The engine names the context its in-process script runs under. `runner.run` binds the schedule's
# actor before it calls the engine, but a process that drives the engine directly binds none; the
# script must still run as `schedule:<id>`, under an environment the SDK's `git` can be handed —
# before this, such a script listed no skill names at all and failed in `require_actor`.
def _engine_root() -> tuple[engine.RunnerRoot, ClientSet]:
    config = _runner_boot()
    machine = ConfiguredMachineIdentity(config)
    image = process_context.loaded_code_image()
    gate = ProcessDbGate(
        process=process_name(), version=CodeVersion(image).get, end_process=hard_exit
    )
    home = meiva_home()

    def database() -> Database:
        return Database.from_settings(
            gate=gate, direct_url=ClusterDirectUrl(machine.host, home=home)
        )

    clients = clients_over(config, machine, database)
    root = engine.RunnerRoot(
        database=database(),
        inputs=ScheduleInputs(database, clients.event_pipeline, image, machine=machine, home=home),
        plugins=engine.PluginInputs(config=config, machine=machine, clients=clients),
        policy=StallPolicy(timeout_s=60.0),
        home=home,
        main_thread_id=threading.get_ident(),
    )
    return root, clients


def test_the_engine_names_the_script_context_when_no_caller_bound_one(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    marker = unit_home / "unnamed.txt"
    script = (
        "from pathlib import Path\n"
        "from meiva.sdk_surface import process_context\n"
        "from meiva.sdk_surface.agent_identity import require_actor\n"
        "context = process_context.current_context()\n"
        f"Path({str(marker)!r}).write_text(\n"
        "    repr((require_actor(), context.require_environ().get('MEIVA_HOME'),\n"
        "          str(context.require_meiva_home())))\n"
        ")\n"
    )
    sid = _schedule(db_conn, script)
    root, clients = _engine_root()
    previous = process_context.unbind_context()
    try:
        assert engine.run(sid, None, root) == 0
    finally:
        clients.close(pipeline_timeout=2)
        # meiva:note
        # The engine binds the context it hands the load, so this test puts the process back where
        # it found it rather than leaving a schedule actor bound for the next test.
        process_context.unbind_context()
        if previous is not None:
            process_context.bind_context(previous)

    # meiva:note
    # the test's own repr.
    actor, environ_home, home = eval(marker.read_text())  # noqa: S307
    assert actor == f"schedule:{sid}"
    assert environ_home == str(unit_home)
    assert home == str(unit_home)


# meiva:note
# A schedule that speaks as an agent: the owner is created first (the column is a foreign key),
# then the schedule names it. Nothing else about the row differs from `_schedule`.
def _owned_schedule(conn: psycopg.Connection, script: str) -> tuple[int, int]:
    owner = create_agent(conn)
    row = conn.execute(
        "INSERT INTO schedules (name, script, command, machine, owner_agent_id) "
        "VALUES ('owned', %s, 'python schedule.py', 'gateway', %s) RETURNING id",
        (script, owner),
    ).fetchone()
    conn.commit()
    assert row is not None
    return int(row[0]), owner


def test_an_owned_schedule_speaks_as_its_agent_and_cannot_drive_its_loop(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    marker = unit_home / "owned.txt"
    script = (
        "import meiva\n"
        "from pathlib import Path\n"
        "from meiva.sdk_surface.agent_identity import require_actor, require_agent_id\n"
        "seen = {'agent_id': meiva.self.AGENT_ID, 'actor': require_actor(),\n"
        "        'required': require_agent_id(),\n"
        "        'owns_loop': meiva.context.identity.owns_loop}\n"
        "for name, call in (('terminate', meiva.self.terminate),\n"
        "                   ('restart', lambda: meiva.self.restart('probe')),\n"
        "                   ('compact', lambda: meiva.self.compact('probe'))):\n"
        "    try:\n"
        "        call()\n"
        "        seen[name] = 'ALLOWED'\n"
        "    except BaseException as exc:\n"
        "        seen[name] = type(exc).__name__ + ': ' + str(exc)\n"
        f"Path({str(marker)!r}).write_text(repr(seen))\n"
    )
    sid, owner = _owned_schedule(db_conn, script)

    assert runner.run(sid, config=_runner_boot()) == 0

    # meiva:note
    # the test's own repr.
    seen = eval(marker.read_text())  # noqa: S307
    assert (seen["agent_id"], seen["required"], seen["actor"]) == (owner, owner, f"schedule:{sid}")
    assert seen["owns_loop"] is False
    # meiva:note
    # The negative control: an owned schedule must not be able to compact, restart or terminate
    # the agent it speaks for, and the refusal must be explicit (not a silent no-op).
    for action in ("terminate", "restart", "compact"):
        assert "can only be called from inside an agent process" in seen[action], seen[action]
    # meiva:note
    # The run row names the same agent, so "which run" and "for whom" are one lookup.
    assert db_conn.execute("SELECT agent_id FROM schedule_runs").fetchall() == [(owner,)]


def test_an_ownerless_schedule_has_no_agent_namespaces(
    db_conn: psycopg.Connection, unit_home: Path
) -> None:
    # meiva:note
    # The control case: without an owner the schedule still runs a command-only script, and the
    # per-identity namespaces stay absent. Without this, the owned test above could pass on a
    # surface that loads them for everyone.
    marker = unit_home / "plain.txt"
    script = (
        "import meiva\n"
        "from pathlib import Path\n"
        "from meiva.sdk_surface.agent_identity import require_actor, require_agent_id\n"
        "seen = {'actor': require_actor(), 'agent_id': meiva.self.AGENT_ID}\n"
        "try:\n"
        "    require_agent_id()\n"
        "    seen['require'] = 'ALLOWED'\n"
        "except BaseException as exc:\n"
        "    seen['require'] = type(exc).__name__ + ': ' + str(exc)\n"
        f"Path({str(marker)!r}).write_text(repr(seen))\n"
    )
    sid = _schedule(db_conn, script)

    assert runner.run(sid, config=_runner_boot()) == 0

    # meiva:note
    # the test's own repr.
    seen = eval(marker.read_text())  # noqa: S307
    assert seen["agent_id"] is None
    assert seen["actor"] == f"schedule:{sid}"
    # meiva:note
    # The gate a `meiva.tasks` write would hit: without an owner the schedule cannot attribute the
    # write, so it fails here rather than landing a task nobody owns.
    assert "no established agent identity" in seen["require"], seen["require"]
