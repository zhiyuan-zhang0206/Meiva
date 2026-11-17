# meiva:note
# Schedule only a prepared full update, with one strict quiescence check before down.


from __future__ import annotations

import argparse
import os
import platform
import subprocess
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from ..choices import FleetArgs
from ..down import prepare
from ..inputs import UpdateWindowInputs
from ..session import FailedError, RefusedError, Session
from . import host_timer
from .agents import notify, running_agents
from .plan import initiator_id, run_at


def submit(s: Session, args: FleetArgs, inputs: UpdateWindowInputs, new: str) -> None:
    if args.at is None:
        raise ValueError("missing --at")
    moment = run_at(args.at, inputs.now)
    source = str(args.source)
    own = initiator_id(source)
    directory = Path(args.log_dir).resolve() / "scheduled"
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    record_path = directory / (inputs.operation_id + ".json")
    record: dict[str, object] = {
        "operation_id": inputs.operation_id,
        "run_at": moment.isoformat(),
        "initiator": source,
        "new": new,
        "repo": str(inputs.repo),
        "arguments": asdict(args),
        "state": "pending",
    }
    if record_path.exists():
        raise ValueError("operation identity already exists")
    host_timer.save(record_path, record)
    message = (
        f"[Scheduled Cluster Update at {moment.isoformat()} by {source}] "
        f"operation {inputs.operation_id}; {args.message_text or ''}; "
        "please finish your current turn and become idle/terminated"
    )
    environment = {
        key: value
        for key, value in inputs.env.items()
        if key in {"HOME", "PATH", "USER", "SSH_AUTH_SOCK", "TMPDIR", "LANG"}
    }
    environment.update(MEIVA_OPERATION_ID=inputs.operation_id, MEIVA_OPERATION_INITIATOR=source)
    command = [inputs.python, "-m", "cli.fleet_update.scheduled_worker", str(record_path)]
    # meiva:note
    # The operator checkout and interpreter must survive the window. They are deliberately
    # distinct from the serving checkouts switched over SSH by down.
    environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[3])
    try:
        targets = [agent for agent in running_agents(s, args.gateway) if agent != own]
        notify(s, args.gateway, targets, source, message)
        if moment <= datetime.now(UTC):
            raise ValueError("--at deadline elapsed during preparation; submit a later time")
        host_timer.arm(
            record_path,
            command=command,
            moment=moment,
            env=environment,
            system=platform.system(),
            uid=os.getuid(),
        )
    except (FailedError, OSError, ValueError, subprocess.SubprocessError):
        record["state"] = "registration-failed"
        host_timer.save(record_path, record)
        raise
    s.say(
        f"scheduled: {record_path}; operation_id={inputs.operation_id}; run_at={moment.isoformat()}; initiator={source}"
    )
    s.say(f"inspect: meiva update status {record_path}; cancel: meiva update cancel {record_path}")


def manage(args: argparse.Namespace, inputs: UpdateWindowInputs) -> int:
    fields: dict[str, object] = vars(args)
    return host_timer.manage(
        Path(str(fields["record"])),
        str(fields["half"]),
        env=inputs.env,
        system=platform.system(),
        uid=os.getuid(),
    )


def validate(args: FleetArgs, now: datetime) -> str | None:
    try:
        if args.half != "run" or args.dry_run or args.source is None or args.at is None:
            raise ValueError("--at requires run --execute and explicit --source user|agent:<id>")
        initiator_id(args.source)
        run_at(args.at, now)
    except ValueError as exc:
        return str(exc)
    return None


def prepare_submit(
    s: Session, args: FleetArgs, inputs: UpdateWindowInputs | None, new: str | None
) -> None:
    if inputs is None or new is None:
        raise RefusedError("scheduled submission requires CLI entry inputs")
    home = inputs.env.get("HOME")
    if home is not None:
        serving = (Path(home) / ".meiva" / "source").resolve()
        operator = Path(__file__).resolve().parents[3]
        if operator == serving or Path(inputs.python).is_relative_to(serving):
            raise RefusedError(
                "schedule from an independent operator checkout and interpreter; this serving checkout is switched by the update"
            )
    try:
        prepare(s, args, new)
        submit(s, args, inputs, new)
    except (FailedError, ValueError, OSError, subprocess.SubprocessError) as exc:
        raise RefusedError(f"scheduled update refused: {exc}") from exc
