# meiva:note
# The worker of the shutdown tests: it blocks the way a long dump or restore does, with a
# real child process and a real throwaway Postgres, so the daemon's SIGTERM has a whole
# process family to reap.
#
# `MEIVA_TEST_BACKUP_MODE` picks the behavior: `block` waits on a child, `stubborn` ignores
# SIGTERM itself, `restore` and `restore-stubborn` do the same after starting a throwaway
# Postgres, and `roundtrip` starts one and exits cleanly. A dump request in a restore mode
# completes at once, so the daemon proceeds to its restore drill.

from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import psycopg

from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.cluster.dataplane.pg_tools import throwaway_postgres
from services.backup.scheduler.operation.worker_process import worker_request

_BLOCKER = Path(__file__).with_name("shutdown_blocker.py")


# meiva:note
# The blocker is a fixed script of this directory started with the interpreter's own
# argv, so the subprocess suppression is the harness's.
def _block(root: Path, *, stubborn: bool) -> None:
    if stubborn:
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    (root / "worker").write_text(str(os.getpid()))
    subprocess.run(  # noqa: S603
        [sys.executable, str(_BLOCKER), str(root / "child"), *(["stubborn"] if stubborn else [])],
        check=True,
        timeout=600,
    )


def _dump(work: Path, request: dict[str, object], result: Path) -> None:
    stamp = datetime.fromisoformat(str(request["now"])).strftime("%Y%m%dT%H%M%SZ")
    artifact = work / "artifact" / f"stubdb-{stamp}.dump.enc"
    artifact.parent.mkdir(exist_ok=True)
    artifact.write_bytes(b"encrypted " + stamp.encode())
    digest = hashlib.sha256(artifact.read_bytes()).hexdigest()
    result.write_text(json.dumps({"artifact": artifact.name, "sha256": digest}))


def _restore(root: Path, mode: str, base: Path, requested_base: object) -> None:
    assert isinstance(requested_base, str)
    captured = Path(requested_base)
    assert captured.is_absolute() and captured.samefile(base), requested_base
    with throwaway_postgres(base=base, foreground=True, config=ThrowawayConfig(base)) as url:
        with psycopg.connect(url) as conn:
            row = conn.execute("SHOW data_directory").fetchone()
            assert row is not None
            data = Path(row[0])
            (root / "postgres").write_text(
                data.joinpath("postmaster.pid").read_text().splitlines()[0]
            )
            (root / "pgdata").write_text(str(data))
        if mode != "roundtrip":
            _block(root, stubborn=mode == "restore-stubborn")


def main() -> None:
    request, result = worker_request(sys.argv)
    work = result.parent
    root = Path(os.environ["MEIVA_TEST_BACKUP_ROOT"])
    mode = os.environ["MEIVA_TEST_BACKUP_MODE"]
    base = Path(os.environ["MEIVA_TEST_BACKUP_POSTGRES_BASE"])
    if request["kind"] == "dump":
        if mode.startswith(("restore", "roundtrip")):
            _dump(work, request, result)
            return
        staged = work / "artifact"
        staged.mkdir()
        (staged / "stage.dump.partial").write_bytes(b"PLAINTEXT")
        _block(root, stubborn=mode == "stubborn")
        return
    scratch = work / "scratch"
    scratch.mkdir(mode=0o700)
    (scratch / "backup.dump").write_bytes(b"PLAINTEXT")
    _restore(root, mode, base, request["pg_throwaway_base"])
    result.write_text('{"restored":true}')


if __name__ == "__main__":
    main()
