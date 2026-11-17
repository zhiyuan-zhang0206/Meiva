"""OS-owned entry: wait outside Meiva, claim once, then perform a strict update attempt."""

from __future__ import annotations

import argparse
import fcntl
import os
import sys
import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel

from . import run_window
from .choices import FleetArgs
from .plan.operation import OperationIdentity
from .scheduling.agents import notify
from .scheduling.host_timer import save
from .scheduling.plan import initiator_id
from .session import Pacing, Session
from .timers import ProcessTimers


class Record(BaseModel):
    operation_id: str
    run_at: datetime
    initiator: str
    new: str
    repo: Path
    arguments: FleetArgs
    state: str
    error: str | None = None


def execute(path: Path) -> int:
    record = Record.model_validate_json(path.read_text())
    if record.state != "pending":
        return 0
    delay = (record.run_at - datetime.now(UTC)).total_seconds()
    if delay > 0:
        time.sleep(delay)
    with path.with_suffix(".lock").open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        record = Record.model_validate_json(path.read_text())
        if record.state != "pending":
            return 0
        record.state = "executing"
        save(path, record.model_dump(mode="json"))
        args = replace(record.arguments, at=None)
        environment = dict(os.environ)
        environment.pop("MEIVA_AGENT_ID", None)
        now = datetime.now(UTC)
        rc = 1
        try:
            rc = run_window(
                args,
                repo=record.repo,
                env=environment,
                now=now,
                pacing=Pacing(),
                timers=ProcessTimers(),
                operation_id=record.operation_id,
                initiator=record.initiator,
                pinned_new=record.new,
                strict=True,
            )
        except (OSError, ValueError) as exc:
            record.error = str(exc)
            print(f"scheduled update failed: {exc}", flush=True)
        finally:
            record.state = "complete" if rc == 0 else "failed"
            save(path, record.model_dump(mode="json"))
        own = initiator_id(record.initiator)
        if own is not None and rc != 2:
            s = Session(
                log_dir=args.log_dir,
                half="receipt",
                dry_run=False,
                repo=record.repo,
                env=environment,
                now=datetime.now(UTC),
                pacing=Pacing(),
                timers=ProcessTimers(),
                operation=OperationIdentity(
                    record.operation_id, record.initiator, "overridden", "scheduled receipt"
                ),
            )
            try:
                notify(
                    s,
                    args.gateway,
                    [own],
                    record.initiator,
                    f"Scheduled cluster update {record.operation_id}: exit {rc}; record {path}; state {record.state}.",
                )
            finally:
                s.log.close()
        return rc


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("record", type=Path)
    fields: dict[str, object] = vars(parser.parse_args())
    return execute(Path(str(fields["record"])))


if __name__ == "__main__":
    sys.exit(main())
