# meiva:note
# Supervise one declarative run and bootstrap its gated payload in a fresh process.
# meiva:link services/wake/schedule_manager/docs/runner-composition.md

from __future__ import annotations

import json
import math
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Never

from loguru import logger

from base.config import ConfigBoot
from base.host.atomic_io import write_text_atomic
from base.native_process.process_control import SYSTEM_PROCESS_CONTROL
from base.paths import meiva_home, prod_service_checkout_error
from services.wake.schedule_manager import engine
from services.wake.schedule_manager.run_custody import register_run_member


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
    logger.error("Usage: schedule runner --single-round ... | --payload <path> <fd>")
    raise SystemExit(2)


if __name__ == "__main__":
    main(sys.argv[1:])
