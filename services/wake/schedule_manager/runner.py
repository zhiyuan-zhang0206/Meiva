# meiva:note
# A stale runner revision cannot execute a newer script, while current revision admission is recorded before successful completion.
# meiva:link services/wake/schedule_manager/docs/runner-composition.md

from __future__ import annotations

import json
import math
import os
import random
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Never

from loguru import logger

from base.agents.context import MeivaContext, deferred_event_bus
from base.agents.context.clients import ClientSet
from base.agents.context.identity import AgentIdentity
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.config import ConfigBoot
from base.daemon.schedules.claim import clear_claim, write_claim
from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.host.atomic_io import write_text_atomic
from base.host.exit_codes import hard_exit
from base.native_process.code_version import CodeVersion
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from base.paths import meiva_home, prod_service_checkout_error
from base.telemetry import process_name
from meiva.boot import clients_over
from meiva.sdk_surface import process_context
from services.wake.schedule_manager import engine
from services.wake.schedule_manager.run_custody import register_run_member
from services.wake.schedule_manager.stall_policy import StallPolicy


def _write_single_round_outcome(
    path: Path, *, ok: bool, returncode: int, timed_out: bool, note: str | None
) -> None:
    payload = {
        "ok": ok,
        "returncode": returncode,
        "timed_out": timed_out,
        "note": note,
    }
    write_text_atomic(path, json.dumps(payload, sort_keys=True) + "\n", sync_file=False)


def _hard_kill_own_group(exit_code: int) -> Never:
    try:
        os.killpg(os.getpgrp(), signal.SIGKILL)
    finally:
        # meiva:note
        # SIGKILL normally wins before this executes. The fallback preserves the bound if the
        # group signal was refused after this process lost its session custody.
        os._exit(exit_code)


def _await_launch_gate(gate_fd: int) -> bool:
    try:
        while True:
            try:
                return os.read(gate_fd, 1) == b"\x01"
            except InterruptedError:
                continue
    finally:
        os.close(gate_fd)


def _release_payload_gate(gate_fd: int) -> None:
    if os.write(gate_fd, b"\x01") != 1:
        raise RuntimeError("payload launch gate accepted no byte")


# meiva:note
# Run ``run.py`` once through the SDK bootstrap and leave a restart-adoptable outcome. The child
# command uses the fixed interpreter/module and a payload path rooted in the schedule workspace.
def run_single_round(
    schedule_id: int,
    run_id: int,
    timeout_seconds: int,
    deadline_epoch: float,
    outcome_path: Path,
    gate_fd: int,
    *,
    home: Path,
) -> int:
    del home
    if not _await_launch_gate(gate_fd):
        return 125
    script_path = Path.cwd() / "run.py"

    def interrupted(signum: int, _frame: object) -> None:
        try:
            _write_single_round_outcome(
                outcome_path,
                ok=False,
                returncode=128 + signum,
                timed_out=False,
                note="cancelled",
            )
        finally:
            _hard_kill_own_group(128 + signum)

    previous_term = signal.signal(signal.SIGTERM, interrupted)
    previous_int = signal.signal(signal.SIGINT, interrupted)
    try:
        # meiva:note
        # The payload stays in this supervisor's dedicated process group. If the supervisor is
        # SIGKILLed, its durable run claim still owns every surviving payload process by PGID.
        payload_read_fd, payload_write_fd = os.pipe()
        try:
            child = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    "-m",
                    "services.wake.schedule_manager.runner",
                    "--payload",
                    str(script_path),
                    str(payload_read_fd),
                ],
                cwd=Path.cwd(),
                env=os.environ.copy(),
                stdin=subprocess.DEVNULL,
                pass_fds=(payload_read_fd,),
            )
            os.close(payload_read_fd)
            payload_read_fd = -1
            register_run_member(
                Path.cwd(),
                schedule_id,
                run_id,
                child.pid,
                process_control=SYSTEM_PROCESS_CONTROL,
            )
            _release_payload_gate(payload_write_fd)
        except Exception:
            logger.exception("single-round payload custody failed")
            _hard_kill_own_group(125)
        finally:
            if payload_read_fd >= 0:
                os.close(payload_read_fd)
            os.close(payload_write_fd)
        try:
            returncode = child.wait(timeout=max(0.0, deadline_epoch - time.time()))
        except subprocess.TimeoutExpired:
            note = f"timed out after {timeout_seconds}s"
            try:
                _write_single_round_outcome(
                    outcome_path,
                    ok=False,
                    returncode=124,
                    timed_out=True,
                    note=note,
                )
            finally:
                _hard_kill_own_group(124)
        _write_single_round_outcome(
            outcome_path,
            ok=returncode == 0,
            returncode=returncode,
            timed_out=False,
            note=None if returncode == 0 else f"process exited {returncode}",
        )
        return returncode
    finally:
        _ = signal.signal(signal.SIGTERM, previous_term)
        _ = signal.signal(signal.SIGINT, previous_int)


def _bind_schedule_actor(
    schedule_id: int,
    *,
    owner_agent_id: int | None,
    clients: ClientSet,
    clock_factory: Callable[[], Clock],
    config: ConfigBoot,
    machine: MachineIdentityReader,
    home: Path,
    user_home: Path,
    rng: random.Random,
) -> None:
    process_context.bind_context(
        MeivaContext(
            identity=AgentIdentity(
                agent_id=owner_agent_id,
                # meiva:note
                # A schedule that speaks as an agent is a script carrying that identity, not
                # the agent's own turn path (the same posture a shell session's child script
                # gets). owns_loop stays False so a schedule cannot compact, restart or
                # terminate the agent it speaks for; the actor keeps the provenance
                # "schedule:<id>" either way. With no owner this is unchanged from before:
                # agent_id=None makes owns_loop unobservable.
                owns_loop=owner_agent_id is None,
                actor=f"schedule:{schedule_id}",
            ),
            clients=clients,
            machine=machine,
            clock_factory=clock_factory,
            bus_factory=deferred_event_bus(config, rng=rng, timers=SYSTEM_TIMERS),
            meiva_home=home,
            user_home=user_home,
            environ=config.process_environment(),
            rng=rng,
        )
    )


def run(schedule_id: int, revision: int | None = None, *, config: ConfigBoot | None = None) -> int:
    if config is None:
        config = ConfigBoot()
        config.read_process_environment()
    machine = ConfiguredMachineIdentity(config)
    image = process_context.loaded_code_image()
    version = CodeVersion(image)
    gate = ProcessDbGate(process=process_name(), version=version.get, end_process=hard_exit)
    home = meiva_home()
    # meiva:note
    # The one record that answers "is the process that owns this schedule still running": a
    # session is a login shell and outlives its runner (base/daemon/schedules/claim.py).
    _ = write_claim(home, schedule_id, pid=os.getpid())

    def database() -> Database:
        return Database(
            db_config_from_boot(config),
            gate=gate,
            direct_url=ClusterDirectUrl(machine.host, home=home),
        )

    def clock_factory() -> Clock:
        return Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS)

    clients = clients_over(config, machine, database)
    primary: BaseException | None = None
    try:
        rng = random.SystemRandom()
        # meiva:note
        # The script's `meiva.agents.*` calls attribute to the schedule: bind its actor before
        # the engine opens any history for it.
        _bind_schedule_actor(
            schedule_id,
            owner_agent_id=engine.owner_agent_id(database(), schedule_id),
            clients=clients,
            clock_factory=clock_factory,
            config=config,
            machine=machine,
            home=home,
            user_home=Path.home(),
            rng=rng,
        )
        return engine.run(
            schedule_id,
            revision,
            engine.RunnerRoot(
                database=database(),
                inputs=ScheduleInputs(
                    database, clients.event_pipeline, image, machine=machine, home=home
                ),
                plugins=engine.PluginInputs(config=config, machine=machine, clients=clients),
                policy=StallPolicy(timeout_s=config.view.gateway.schedule_stall_timeout_seconds),
                home=home,
                rng=rng,
                pid=os.getpid(),
                # meiva:note
                # This process entry records the thread the guard must watch once, before it
                # hands the runner to the engine.
                main_thread_id=threading.get_ident(),
            ),
        )
    except BaseException as exc:
        primary = exc
        raise
    finally:
        try:
            clients.close(pipeline_timeout=2)
        except BaseException as cleanup:
            if primary is None:
                raise
            primary.add_note(f"schedule clients cleanup failed: {cleanup!r}")
        # meiva:note
        # Cleared last: until this line the process is still the runner of this schedule, and a
        # record left behind is what tells a reader the process did not exit on its own.
        clear_claim(home, schedule_id)


def _single_round_arguments(argv: Sequence[str]) -> tuple[int, int, int, float, Path, int]:
    if len(argv) != 7:
        raise SystemExit(2)
    schedule_id = int(argv[1])
    run_id = int(argv[2])
    timeout_seconds = int(argv[3])
    deadline_epoch = float(argv[4])
    gate_fd = int(argv[6])
    if (
        schedule_id < 1
        or run_id < 1
        or timeout_seconds < 1
        or not math.isfinite(deadline_epoch)
        or deadline_epoch <= 0
        or gate_fd < 0
    ):
        raise SystemExit(2)
    return schedule_id, run_id, timeout_seconds, deadline_epoch, Path(argv[5]), gate_fd


def main(argv: Sequence[str]) -> None:
    payload_path: Path | None = None
    if len(argv) == 3 and argv[0] == "--payload":
        payload_path = Path(argv[1])
        if not _await_launch_gate(int(argv[2])):
            raise SystemExit(125)
    boot = ConfigBoot()
    boot.boot()
    refusal = prod_service_checkout_error(Path(__file__).resolve().parents[3], home=meiva_home())
    if refusal is not None:
        logger.error("schedule runner refused: {}", refusal)
        raise SystemExit(3)
    if payload_path is not None:
        engine.run_single_round_payload(payload_path)
        raise SystemExit(0)
    if len(argv) >= 1 and argv[0] == "--single-round":
        schedule_id, run_id, timeout_seconds, deadline_epoch, outcome_path, gate_fd = (
            _single_round_arguments(argv)
        )
        raise SystemExit(
            run_single_round(
                schedule_id,
                run_id,
                timeout_seconds,
                deadline_epoch,
                outcome_path,
                gate_fd,
                home=meiva_home(),
            )
        )
    if len(argv) not in (1, 2):
        logger.error(
            "Usage: python -m services.wake.schedule_manager.runner <schedule_id> [revision]"
        )
        raise SystemExit(2)

    revision = int(argv[1]) if len(argv) == 2 else None
    if revision is not None and revision < 0:
        raise SystemExit(2)
    raise SystemExit(run(int(argv[0]), revision, config=boot))


if __name__ == "__main__":
    main(sys.argv[1:])
