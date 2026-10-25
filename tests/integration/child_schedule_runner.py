# meiva:note
# Child process for the schedule-runner cleanup tests: a real runner with a stalled or finished script.
#
# Usage:
# - `child_schedule_runner.py run <root> <schedule id> <mode>` runs the production runner entry; the
#   parent names its stall budget in `MEIVA_SCHEDULE_STALL_TIMEOUT_SECONDS`.
# - `child_schedule_runner.py stall_exit <root> <schedule id> <run id>` runs the stall action alone
#   against the database the parent points it at.
# - `child_schedule_runner.py completion_during_stall <root> <schedule id>` runs the engine on a
#   script that blocks, stalls, and then returns while the stall action is still writing.
#
# Failures are made by the parent in the database (a held row lock), never by replacing code here.
# Imports live inside the scenarios because some modes alter the interpreter before the runner
# module loads.

import sys
import time
from pathlib import Path

# meiva:note
# The stall budget of the scenarios that build their own engine root, and the bound the
# stall action puts on its failure writes.
_BUDGET_S = 0.15
_RECORD_DEADLINE_S = 1.0


# meiva:note
# Install the plugin namespaces before the stall budget starts counting.
#
# Loading them takes longer than the budget these scenarios run under, and the runner's own
# load is then the no-op of an already loaded process.
def _preload_plugins() -> None:
    import meiva
    from base.config import ConfigBoot

    config = ConfigBoot()
    config.read_process_environment()
    meiva.ensure_plugins_loaded(config=config)


def run_runner(schedule_id: int, mode: str) -> None:
    if mode == "missing-module-files":
        import selectors
        import subprocess

        del subprocess.__file__
        del selectors.__file__
    _preload_plugins()
    from services.wake.schedule_manager.runner import run

    raise SystemExit(run(schedule_id))


def _database():
    from base.db import Database
    from base.db.code_version_gate import ProcessDbGate
    from base.native_process.code_version import CodeVersion
    from base.native_process.loaded_commit import LoadedCommit

    image = LoadedCommit.capture()
    gate = ProcessDbGate(process="schedule-cleanup-test", version=CodeVersion(image).get)
    return Database.from_settings(gate=gate), image


def stall_exit(root: Path, schedule_id: int, run_id: int) -> None:
    from services.wake.schedule_manager import engine
    from services.wake.schedule_manager.stall_policy import StallPolicy

    database, _image = _database()
    (root / "started").write_text(str(time.monotonic()))
    engine.stall_action(
        database,
        schedule_id,
        "test stall",
        run_id,
        policy=StallPolicy(timeout_s=_BUDGET_S, record_deadline_s=_RECORD_DEADLINE_S),
    )


# meiva:note
# The script blocks until the parent releases it, so the guard really stalls; the script then
# returns while the stall action is blocked on its first write, and the hard exit still wins.
def completion_during_stall(schedule_id: int) -> None:
    from functools import partial

    from base.agents.context.clients import ClientSet
    from base.cluster.machine.configured import ConfiguredMachineIdentity
    from base.config import ConfigBoot
    from base.daemon.schedules.inputs import ScheduleInputs
    from base.paths import meiva_home
    from base.telemetry.pipeline_build import build_pipeline
    from services.wake.schedule_manager import engine
    from services.wake.schedule_manager.stall_policy import StallPolicy

    _preload_plugins()
    config = ConfigBoot()
    config.read_process_environment()
    machine = ConfiguredMachineIdentity(config)
    database, image = _database()
    clients = ClientSet(
        database=lambda: database,
        pipeline_factory=partial(
            build_pipeline, database=lambda: database, machine=machine, home=meiva_home()
        ),
    )

    root = engine.RunnerRoot(
        database=database,
        inputs=ScheduleInputs(lambda: database, clients.event_pipeline, image, machine=machine),
        plugins=engine.PluginInputs(config=config, machine=machine, clients=clients),
        policy=StallPolicy(timeout_s=_BUDGET_S, record_deadline_s=_RECORD_DEADLINE_S),
        home=meiva_home(),
    )
    raise SystemExit(engine.run(schedule_id, None, root))


scenario, *arguments = sys.argv[1:]
if scenario == "run":
    run_runner(int(arguments[1]), arguments[2])
elif scenario == "stall_exit":
    stall_exit(Path(arguments[0]), int(arguments[1]), int(arguments[2]))
elif scenario == "completion_during_stall":
    completion_during_stall(int(arguments[1]))
elif scenario == "sleep":
    time.sleep(60)
else:
    raise ValueError(f"unknown scenario {scenario!r}")
