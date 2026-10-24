"""Scheduled logical backup workers with private staging and validated publication."""

from __future__ import annotations

import dataclasses
import hashlib
import os
import shutil
import sys
import tempfile
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal, assert_never

from base.cluster.dataplane.direct_url import ClusterDirectUrl
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.cluster.machine import MachineIdentity
from base.cluster.machine.boot import machine_from_boot
from base.config import ConfigBoot
from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.db.config import db_config_from_boot
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from base.telemetry.pipeline_build import build_pipeline
from services.backup.layout import BackupTree
from services.backup.scheduler.commit_plan import (
    digest_problem,
    dump_result_problem,
    link_route,
    restore_result_problem,
    staged_problem,
)
from services.backup.scheduler.operation.staging import (
    OperationKind,
    publish_result,
)
from services.backup.scheduler.operation.worker_process import (
    CompletedOperation,
    run_operation,
    worker_request,
)
from services.backup.scheduler.settings import DumpSettings, SchedulerSettings

Job = Literal["dump", "restore"]

WORKER_MODULE = "services.backup.scheduler.worker"


# meiva:note
# Which worker module a job launches and the environment it inherits. The scheduler
# daemon builds it once at its root; a test names a worker module of its own.
@dataclass(frozen=True)
class WorkerLaunch:
    module: str
    env: dict[str, str]


def dump_kind(tree: BackupTree) -> OperationKind:
    return OperationKind("logical-dump", tree.controls("dump"), staged_artifacts=True)


def restore_drill_kind(tree: BackupTree, *, throwaway: ThrowawayConfig) -> OperationKind:
    return OperationKind(
        "logical-restore-drill",
        tree.controls("restore-drill"),
        plaintext_scratch=True,
        throwaway=throwaway,
    )


def _sha256(path: Path) -> str:
    with path.open("rb") as artifact:
        return hashlib.file_digest(artifact, "sha256").hexdigest()


async def run_job(
    kind: Job,
    *,
    settings: SchedulerSettings,
    tree: BackupTree,
    launch: WorkerLaunch,
    now: datetime | None = None,
) -> None:
    """Validate and publish a zero-exit worker result."""
    request: dict[str, object] = {"kind": kind, "now": now.isoformat() if now is not None else None}
    match kind:
        case "restore":
            override = settings.throwaway_base
            request["pg_throwaway_base"] = None if override is None else str(override)
            operation_kind = restore_drill_kind(tree, throwaway=ThrowawayConfig(override))
        case "dump":
            operation_kind = dump_kind(tree)
        case _:
            assert_never(kind)
    completed = await run_operation(launch.module, request, kind=operation_kind, env=launch.env)
    match kind:
        case "dump":
            # meiva:note
            # Hashing and linking a multi-GiB artifact is bounded local I/O; the
            # scheduler's health server shares this loop.
            await completed.commit(lambda: _commit_dump(completed, tree=tree, keep=settings.keep))
        case "restore":
            await completed.commit(lambda: _accept_restore(completed))
        case _:
            assert_never(kind)


def _commit_dump(completed: CompletedOperation, *, tree: BackupTree, keep: int) -> Path:
    problem = dump_result_problem(completed.result)
    if problem is not None:
        raise RuntimeError(problem)
    name, digest = str(completed.result["artifact"]), str(completed.result["sha256"])
    return commit_scheduled_backup(completed.work / "artifact" / name, digest, tree=tree, keep=keep)


def _accept_restore(completed: CompletedOperation) -> None:
    problem = restore_result_problem(completed.result)
    if problem is not None:
        raise RuntimeError(problem)


def commit_scheduled_backup(staged: Path, digest: str, *, tree: BackupTree, keep: int) -> Path:
    """Publish only the exact completed worker artifact; never overwrite one.

    The caller invokes this only after the worker returned a zero
    exit. A backup directory on another filesystem gets a verified private copy
    published the same exclusive way. Any refusal leaves the staged artifact in
    private staging until cleanup.
    """
    from base.host.private_storage import ensure_private_file
    from base.paths.private_dir import ensure_private_dir
    from services.backup.dump import prune_after_publish

    problem = staged_problem(
        staged.name, is_symlink=staged.is_symlink(), is_file=staged.is_file()
    ) or digest_problem(_sha256(staged), digest)
    if problem is not None:
        raise RuntimeError(problem)
    directory = ensure_private_dir(tree.managed)
    target = directory / staged.name
    try:
        os.link(staged, target)  # Exclusive publication; a prior artifact is never replaced.
    except OSError as exc:
        route = link_route(exc.errno)
        match route:
            case "raise":
                raise
            case "copy":
                publish_copy(staged, target, digest)
            case _:
                assert_never(route)
    ensure_private_file(target)
    staged.unlink()
    prune_after_publish(target, tree=tree, keep=keep)
    return target


# meiva:note
# A verified private copy of `staged` that stays open until the caller is done with it.
#
# The copy stays open, so a backup run sweeping this directory (it removes only
# intermediates no process holds open) never takes it mid-publication; a controller
# killed meanwhile leaves a closed copy that the next backup run sweeps.
@contextmanager
def private_copy(staged: Path, directory: Path, name: str, digest: str) -> Generator[Path]:
    fd, raw = tempfile.mkstemp(prefix=f".{name}.", suffix=".copy", dir=directory)
    copy = Path(raw)
    try:
        os.fchmod(fd, 0o600)
        with staged.open("rb") as source, os.fdopen(fd, "w+b") as output:
            shutil.copyfileobj(source, output, 8 * 1024 * 1024)
            output.flush()
            os.fsync(output.fileno())
            output.seek(0)
            if hashlib.file_digest(output, "sha256").hexdigest() != digest:
                raise RuntimeError(
                    "scheduled backup copy differs from the completed worker's artifact"
                )
            yield copy
    finally:
        copy.unlink(missing_ok=True)


# meiva:note
# Cross-filesystem publication with the same exclusive, verified result.
def publish_copy(staged: Path, target: Path, digest: str) -> None:
    with private_copy(staged, target.parent, target.name, digest) as copy:
        os.link(copy, target)
    dirfd = os.open(target.parent, os.O_RDONLY)
    try:
        os.fsync(dirfd)
    finally:
        os.close(dirfd)


def execute(
    request: dict[str, object],
    work: Path,
    *,
    settings: DumpSettings,
    tree: BackupTree,
    database: Callable[[], Database],
    database_for_url: Callable[[str], Database],
) -> dict[str, object]:
    kind = request.get("kind")
    if kind not in ("dump", "restore"):
        raise ValueError("invalid logical backup request")
    expected: set[str] = {"kind", "now"}
    if kind == "restore":
        expected.add("pg_throwaway_base")
    if set(request) != expected:
        raise ValueError("invalid logical backup request")
    job: Job = kind
    match job:
        case "dump":
            # meiva:note
            # Imported where used: the scheduler daemon imports this module and must not carry
            # the OSS SDK; this worker, which publishes off-site, loads it.
            from services.backup.artifact import offsite
            from services.backup.dump import dump_source, run_backup

            stamp = request["now"]
            if not isinstance(stamp, str):
                raise TypeError("logical dump requires its captured timestamp")
            artifact = run_backup(
                datetime.fromisoformat(stamp),
                tree=tree,
                db_url=dump_source(
                    database(), is_remote=settings.is_remote, plane_url=settings.plane_url
                ),
                keep=settings.keep,
                offsite_target=offsite.configured_target(
                    settings.offsite_endpoint,
                    settings.offsite_bucket,
                    settings.offsite_credentials_file,
                ),
                staging=work / "artifact",
            )
            return {"artifact": artifact.name, "sha256": _sha256(artifact)}
        case "restore":
            from services.backup.restore_drill import newest_artifact, run_drill

            if request["now"] is not None:
                raise ValueError("unknown logical backup operation")
            raw_base = request["pg_throwaway_base"]
            if raw_base is not None and not isinstance(raw_base, str):
                raise TypeError("logical restore requires a string or null throwaway base")
            throwaway = ThrowawayConfig(None if raw_base is None else Path(raw_base))
            artifact = newest_artifact(tree)
            scratch = work / "scratch"
            scratch.mkdir(mode=0o700)
            run_drill(
                artifact,
                tree=tree,
                foreground=True,
                scratch_root=scratch,
                database_for_url=database_for_url,
                throwaway=throwaway,
            )
            return {"restored": True}
        case _:
            assert_never(job)


# meiva:note
# A handle on the config as its owner holds it now; `url` redirects it to a scratch cluster.
def worker_database(
    config: ConfigBoot,
    machine: MachineIdentity,
    gate: ProcessDbGate,
    *,
    url: str | None = None,
) -> Database:
    db_config = db_config_from_boot(config)
    if url is not None:
        db_config = dataclasses.replace(db_config, db_url=url)
    return Database(db_config, gate=gate, direct_url=ClusterDirectUrl(machine.host))


def main() -> None:
    request, output = worker_request(sys.argv)
    from base.telemetry.process_logging import init_gateway_process

    # The store-verified publish ACK is an INFO record: route it to the log sinks.
    config = ConfigBoot()
    config.boot()
    machine = machine_from_boot(config)
    process_home = meiva_home().resolve()
    image = LoadedCommit.capture()
    version = CodeVersion(image)
    gate = ProcessDbGate(version=version.get, process="pg-backup-worker")

    def database() -> Database:
        return worker_database(config, machine, gate)

    def database_for_url(url: str) -> Database:
        return worker_database(config, machine, gate, url=url)

    pipeline = build_pipeline(database=database, machine=machine, home=process_home)
    init_gateway_process(
        name="pg-backup-worker",
        producer=lambda: pipeline,
        machine_reader=machine.name,
        image=image,
    )
    publish_result(
        output,
        execute(
            request,
            output.parent,
            settings=DumpSettings.read(config),
            tree=BackupTree(process_home),
            database=database,
            database_for_url=database_for_url,
        ),
    )


if __name__ == "__main__":
    from base.log import prepare_process_logging

    prepare_process_logging()
    main()
