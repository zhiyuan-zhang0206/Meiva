"""Locate native Postgres binaries across the two supported host platforms,
and spin up throwaway clusters for tests.

macOS runs Homebrew's keg-only `postgresql@17` (binaries are not symlinked
onto PATH, so the full keg path is required); Linux runs the apt
`postgresql-17` layout. Shared by the
per-cluster data-plane bring-up (`cli/commands/data_plane/cluster_instance.py`), the local
backup path (`services/backup/dump.py`), and the throwaway clusters the test suite
(`base/deploy/tests/containers.py`), migration smoke (`scripts/ci/migration_smoke.py`), and eval
fixtures spin up.

Throwaway clusters are also **self-limiting when their owner is killed** — see
the registry/sweep block below `throwaway_postgres`.
"""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import socket
import subprocess
import tempfile
from collections.abc import Generator, Iterator
from contextlib import closing, contextmanager
from pathlib import Path
from typing import NamedTuple, TypedDict, TypeGuard

from base.cluster.dataplane import pg_throwaway_base as _throwaway_base
from base.cluster.dataplane.pg_foreground import (
    start_foreground_postgres,
    stop_foreground_postgres,
    wait_foreground_postgres,
)
from base.cluster.dataplane.pg_plan import (
    PG_SHM_MMAP_OK_ON_MACOS,
    PG_TZ_ARGS,
    build_pg_shm_args,
    build_pg_start_env,
)
from base.cluster.dataplane.pg_stall_watchdog import (
    StallWatchdogConfig,
    fixture_log_artifact_dir,
    stall_guard,
)
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.log import logger
from base.native_process.child_env import daemon_process_env
from base.native_process.os_platform import is_macos


def pg_shm_args() -> str:
    return build_pg_shm_args(macos=is_macos(), mmap_ok_on_macos=PG_SHM_MMAP_OK_ON_MACOS)


def pg_tz_args() -> str:
    return PG_TZ_ARGS


def pg_start_env() -> dict[str, str]:
    return build_pg_start_env(daemon_process_env(), macos=is_macos())


# meiva:note
# Prefix of every throwaway instance directory this module creates. Load-bearing:
# the sweep below reaps ONLY `<a throwaway root>/<this prefix>*` directories.
THROWAWAY_PREFIX = "meiva-pg-"


def pg_tool(
    name: str,
    *,
    home: Path,
    bin_dir: Path | None = None,
    platform_bin_dir: Path | None = None,
    path_env: str | None = None,
) -> Path:
    if bin_dir is not None and (bin_dir / name).exists():
        return bin_dir / name
    from base.cluster.dataplane.runtime_binaries import vendored_pg_bin_dir

    vendored = vendored_pg_bin_dir(home)
    if vendored is not None and (vendored / name).exists():
        return vendored / name
    if platform_bin_dir is not None:
        platform_path: Path | None = platform_bin_dir / name
    else:
        from base.host.system.backend import get_backend

        platform_path = get_backend().pg_binary_path(name)
    if platform_path is not None and platform_path.exists():
        return platform_path
    found = shutil.which(name) if path_env is None else shutil.which(name, path=path_env)
    if found:
        return Path(found)
    host = "darwin" if is_macos() else "linux"
    raise RuntimeError(
        f"Cannot locate pg tool {name!r} on {host} — "
        "install PostgreSQL 17 or set MEIVA_PG_* (MEIVA_PG_BIN etc.) for a custom pg install"
    )


def has_pg_tool(
    name: str,
    *,
    home: Path,
    bin_dir: Path | None = None,
    platform_bin_dir: Path | None = None,
    path_env: str | None = None,
) -> bool:
    try:
        pg_tool(
            name,
            bin_dir=bin_dir,
            home=home,
            platform_bin_dir=platform_bin_dir,
            path_env=path_env,
        )
    except RuntimeError:
        return False
    return True


def free_port() -> int:
    """Ask the OS for an unused localhost TCP port. The socket is closed
    immediately, so the port is only a PROBE — it is not reserved; `allocate_port`
    makes the reservation atomic."""
    with closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# ── Orphaned throwaway clusters: owner lock + start-of-run sweep ──
#
# A throwaway postmaster is DETACHED — `pg_ctl` exits as soon as it is up, so the
# postmaster is already reparented away from the run that asked for it. Killing the
# owner (Ctrl-C, SIGKILL, an agent dying mid-run) therefore leaves it running with
# nothing left to stop it, and **no teardown path can fix that**: a SIGKILLed
# process runs no `finally`, no `atexit` handler and no signal handler. Each
# survivor holds one System V shared-memory segment (Postgres' startup interlock)
# and macOS ships `kern.sysv.shmmni=32`, so after ~31 interrupted runs the box
# wedges and NO cluster — test or real — can start at all.
#
# The durable mechanism is therefore a sweep at the start of the NEXT spin-up:
# every throwaway instance drops an owner lock INSIDE its own instance dir, and the
# next `throwaway_postgres` reaps the instances whose owner is provably gone.
#
# Liveness oracle: an exclusive `flock` the owner holds on that lock file for the
# instance's whole life. The kernel releases it when the owner dies, however it
# dies. A sweeper that takes `LOCK_EX | LOCK_NB` has therefore PROVEN that no live
# process owns that instance — it never compares pids, so a pid recycled onto an
# unrelated process cannot make a live instance look dead. The lock fd is
# close-on-exec (Python's default), so a test subprocess does not inherit the lock
# and pin a finished instance. Being both the deadness proof and the reap lock, it
# also makes concurrent sweepers (every xdist worker calls this) safe for free.
#
# The lock lives in the instance dir rather than in a side registry, which is what
# makes two otherwise-real failure modes unreachable:
#
# - **It cannot outlive, or be outlived by, the cluster it describes.** A side
#   registry can be pruned by a tmp cleaner while the instance it names keeps
#   running, and that instance is then permanently unsweepable — this leak class,
#   reopened by another route. Here the lock and the data dir are the same
#   directory with the same age, so a cleaner that can delete the lock is already
#   deleting files under a live postmaster (a far louder failure, and one that
#   predates any of this).
# - **No cross-user contention.** `mkdtemp` creates the instance dir `0700`, so on
#   a shared tmpfs (`/dev/shm` on Linux, `/var/tmp` on the disk fallback) another
#   user's instances are invisible to
#   this user's glob and vice versa — correctly, since neither could stop the
#   other's postmaster anyway. A single well-known registry dir owned by whoever
#   ran tests first would instead have made the SECOND user's every run fail on an
#   unwritable directory.
#
# Safety — a real cluster can never be selected. The reap target is not read from
# the lock's *contents*; it is the lock file's own parent directory, re-verified
# against the shape this module itself creates (`resolved_throwaway_dir`): the name
# carries `THROWAWAY_PREFIX`, the resolved directory is a direct child of the
# throwaway root under its own name, and `data` is not a symlink. For a real
# cluster to be selected its `$MEIVA_HOME` would have to BE an `meiva-pg-*` child of
# the throwaway root and keep its data dir at `data` instead of `pg` — two
# independent misses — and no symlink can redirect a verified path back out. The
# only privileged act is `pg_ctl -D <verified data dir> stop`, which takes the pid
# it signals from that data dir's own `postmaster.pid`: no pid this module recorded
# is ever signalled.

# Owner lock, one per instance dir, beside that instance's `data/` and `pg.log`.
OWNER_LOCK_NAME = "owner.lock"
# The claim's private name. The claim file is created, flocked, and written under
# it, then renamed onto `OWNER_LOCK_NAME` (same inode, lock intact): no glob
# matches it, so a sweep can only ever see the final name already-locked (skip) or
# absent (not yet claimed) — never a claim it would judge stale mid-flight (#3629).
CLAIM_LOCK_NAME = "owner.lock.claim"


def _throwaway_locks(config: ThrowawayConfig) -> list[Path]:
    return sorted(
        lock
        for root in _throwaway_base.throwaway_roots(
            config.override, default_root=config.default_root
        )
        for lock in root.glob(f"{THROWAWAY_PREFIX}*/{OWNER_LOCK_NAME}")
    )


class Registration(NamedTuple):
    """The held flock fd + its lock path. `fd` stays open for the instance's whole
    life: closing it is what tells the next sweep the owner is gone."""

    fd: int
    lock: Path


def claim_throwaway_lock(instance_dir: Path, port: int) -> Registration:
    import fcntl

    claim = instance_dir / CLAIM_LOCK_NAME
    fd = os.open(claim, os.O_RDWR | os.O_CREAT | os.O_TRUNC, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    os.write(fd, json.dumps({"port": port}).encode())
    return Registration(fd, claim)


def publish_throwaway_lock(claim_registration: Registration) -> Registration:
    lock = claim_registration.lock.with_name(OWNER_LOCK_NAME)
    claim_registration.lock.rename(lock)
    return Registration(claim_registration.fd, lock)


def register_throwaway(instance_dir: Path, port: int) -> Registration:
    return publish_throwaway_lock(claim_throwaway_lock(instance_dir, port))


def unregister_throwaway(registration: Registration) -> None:
    """Release the flock and drop the lock file — the instance is torn down. The
    instance dir (and with it the lock) is usually already gone by this point; the
    close is what matters, and it cannot fail."""
    os.close(registration.fd)
    registration.lock.unlink(missing_ok=True)


def resolved_throwaway_dir(instance_dir: Path, *, config: ThrowawayConfig) -> Path | None:
    if not instance_dir.name.startswith(THROWAWAY_PREFIX) or not instance_dir.is_dir():
        return None
    resolved = instance_dir.resolve()
    roots = {
        root.resolve()
        for root in _throwaway_base.throwaway_roots(
            config.override, default_root=config.default_root
        )
    }
    if resolved.parent not in roots or resolved.name != instance_dir.name:
        return None
    if (resolved / "data").is_symlink():
        return None
    return resolved


# meiva:note
# The owner.lock body this module writes: diagnostics + allocation registry.
class _OwnerRecord(TypedDict):
    port: int


def _is_owner_record(value: object) -> TypeGuard[_OwnerRecord]:
    return isinstance(value, dict)


def _read_lock_body(fd: int) -> _OwnerRecord | None:
    """The JSON owner record from a lock fd, or None when unreadable. A run killed
    between creating the lock and writing it leaves a truncated body; that is a
    crash artifact, not a caller mistake, so it degrades to None rather than
    aborting the sweep or the port registry."""
    os.lseek(fd, 0, os.SEEK_SET)
    try:
        rec = json.loads(os.read(fd, 4096))
    except (ValueError, UnicodeDecodeError):
        return None
    if not _is_owner_record(rec):
        return None
    return rec


def _owner_detail(fd: int) -> str:
    """Owner info from the lock body, for the reap log line. Diagnostics only — the
    reap target is the lock file's own parent."""
    rec = _read_lock_body(fd)
    if rec is None:
        return ""
    with contextlib.suppress(KeyError):
        return f" (port {rec['port']})"
    return ""


def _reap_locked(lock: Path, fd: int, *, config: ThrowawayConfig) -> int:
    """Reap the instance holding `lock` — its flock is held here, so its owner is
    provably gone. Returns 1 if an instance was removed."""
    if os.fstat(fd).st_nlink == 0:
        return 0  # a racing sweeper already reaped it between our open and our lock
    instance_dir = resolved_throwaway_dir(lock.parent, config=config)
    if instance_dir is None:
        # Nothing safely reapable: the lock sits somewhere that is not one of this
        # module's instance dirs. Drop the stray lock, touch nothing else.
        lock.unlink(missing_ok=True)
        return 0
    detail = _owner_detail(fd)
    data = instance_dir / "data"
    if (data / "PG_VERSION").is_file():
        stop = subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + a verified data dir
            [
                pg_tool("pg_ctl", bin_dir=config.bin_dir, home=config.home),
                "-D",
                str(data),
                "-m",
                "immediate",
                "stop",
            ],
            check=False,
            capture_output=True,
        )
        if stop.returncode != 0:
            logger.warning(
                "reaping throwaway postgres {dir}: pg_ctl stop exited {rc}; a surviving "
                "postmaster, if any, now writes into a removed data directory: {err}",
                dir=str(instance_dir),
                rc=stop.returncode,
                err=(stop.stderr or b"").decode(errors="replace").strip()[:400],
                event="throwaway_pg_reap",
            )
    shutil.rmtree(instance_dir, ignore_errors=True)
    logger.info(f"reaped orphaned throwaway postgres {instance_dir}{detail}")
    return 1


def sweep_orphaned_throwaway_clusters(*, config: ThrowawayConfig) -> int:
    """Stop and delete every throwaway cluster whose owning process is gone, and
    return how many were reaped. Called at the top of `throwaway_postgres`, so any
    run that starts one first bounds the leak from every previous killed run.

    Concurrency-safe (xdist workers all call it): the flock that proves deadness is
    the reap lock too, so one instance is reaped once, and a claim mid-flight is not
    observable — `owner.lock` is published only already-locked (see
    `register_throwaway`). On a shared tmpfs another user's instance dirs are
    `0700`, so the glob simply does not see them."""
    import fcntl

    reaped = 0
    for lock in _throwaway_locks(config):
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            continue  # vanished under us, or not ours to open — not ours to reap
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                continue  # a live process still owns this instance — leave it alone
            reaped += _reap_locked(lock, fd, config=config)
        finally:
            os.close(fd)
    return reaped


# ── Throwaway port allocation: serialized, so parallel workers never collide ──
#
# `free_port` alone is a TOCTOU race: it binds port 0 (the OS picks a free port),
# closes the socket, and the caller's postmaster binds that port roughly a second
# later (after initdb). Two xdist workers in the same window can be handed the SAME
# port — the OS sees it free both times — and the second postmaster then fails with
# "Address already in use" (946 ThrowawayPgStartError on one 8-core CI run).
#
# The fix has two halves:
# - Allocation is atomic: under a host-wide flock on the throwaway root directory,
#   a worker probes the OS AND consults a registry of ports held by LIVE clusters
#   (the owner.lock bodies — the same flock liveness oracle as the sweep), then
#   registers its pick before releasing the lock. A port a live cluster is about to
#   bind is refused, so two live clusters can never be handed the same port even
#   though neither postmaster has bound it yet. The registry self-cleans exactly
#   like the sweep: a dead owner's flock is released by the kernel, and its port is
#   then safe — either the orphaned postmaster still holds it (the OS probe refuses
#   it) or it is truly free.
# - A start that loses the race anyway (a non-throwaway process bound the port in
#   the window) is retried on a fresh port.
#
# The flock lives on the root DIRECTORY rather than a lock file: nothing to create
# (so no permission fights between users on a shared /dev/shm), nothing for the
# sweep globs to trip on, and the kernel drops it on process death like any flock.


def _live_throwaway_ports(config: ThrowawayConfig) -> set[int]:
    """Ports reserved by LIVE throwaway clusters — owner flock still held. Dead
    owners' entries are ignored: either the orphaned postmaster still holds the
    port (the OS probe refuses it anyway) or it is truly free. Only this module's
    instance dirs are consulted, so nothing outside the throwaway world can pin a
    port."""
    import fcntl

    ports: set[int] = set()
    for lock in _throwaway_locks(config):
        try:
            fd = os.open(lock, os.O_RDWR)
        except OSError:
            continue  # vanished under us, or not ours to open — not ours to read
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                rec = _read_lock_body(fd)
                if rec is not None and "port" in rec:
                    ports.add(int(rec["port"]))
            # flock acquired → owner dead; its port is reusable, and the sweeper
            # owns reaping the directory — just close and move on.
        finally:
            os.close(fd)
    return ports


def _port_lock_root(config: ThrowawayConfig | None = None) -> Path:
    if config is not None and config.default_root is not None:
        return config.default_root
    return _throwaway_base.default_base()


def allocate_port(
    instance_dir: Path,
    *,
    config: ThrowawayConfig,
    candidates: Iterator[int] | None = None,
) -> tuple[int, Registration]:
    import fcntl

    root_fd = os.open(_port_lock_root(config), os.O_RDONLY)
    try:
        fcntl.flock(root_fd, fcntl.LOCK_EX)
        try:
            live = _live_throwaway_ports(config)
            port_iter = candidates if candidates is not None else iter(config.candidate_ports)
            while True:
                port = next(port_iter, None)
                if port is None:
                    port = free_port()
                if port not in live:
                    break
            return port, register_throwaway(instance_dir, port)
        finally:
            fcntl.flock(root_fd, fcntl.LOCK_UN)
    finally:
        os.close(root_fd)


# Stands in for pg.log content that was never written — postgres died before
# opening the log (initdb refusing, the postmaster never forking) or before
# writing its first line. Recorded rather than skipped: "the log is empty" is
# itself the diagnosis, while an absent artifact is indistinguishable from the
# capture being broken. Worded to stay true whether the file is missing or bare,
# since both land here.
NO_PG_LOG_NOTE = "<no pg.log content — postgres never got far enough to write any>"


def _read_log_tail(log: Path, lines: int = 40) -> str:
    """The last `lines` of a pg.log, for the raised error's message. Best-effort:
    a log that vanished, is unreadable, or is empty degrades to "", which the
    caller renders as `NO_PG_LOG_NOTE` — reading it must never mask the start
    failure it is describing."""
    with contextlib.suppress(OSError):
        if log.is_file():
            return "\n".join(log.read_text(errors="replace").splitlines()[-lines:])
    return ""


class ThrowawayPgStartError(subprocess.CalledProcessError):
    """A throwaway cluster failed initdb or `pg_ctl start`.

    Subclasses CalledProcessError so existing callers keep working, and carries
    the preserved pg.log path + tail so the failure reason survives the tmpfs
    teardown (issue #1037: 738 errors, one pg_ctl refusal, reason unrecoverable
    because the cluster lived in /dev/shm and was deleted on teardown)."""

    def __init__(
        self,
        exc: subprocess.CalledProcessError,
        log: Path,
        preserved: Path | None,
    ) -> None:
        super().__init__(exc.returncode, exc.cmd, output=exc.output, stderr=exc.stderr)
        self.preserved_log = preserved
        self.log_tail = _read_log_tail(log)

    def __str__(self) -> str:
        parts = [super().__str__()]
        if self.preserved_log is not None:
            parts.append(f"pg.log artifact: {self.preserved_log}")
        parts.append(f"--- pg.log tail ---\n{self.log_tail}" if self.log_tail else NO_PG_LOG_NOTE)
        return "\n".join(parts)


def _preserve_failed_fixture_log(
    instance_dir: Path, *, artifact_dir: Path | None = None
) -> Path | None:
    log = instance_dir / "pg.log"
    dest_dir = artifact_dir if artifact_dir is not None else fixture_log_artifact_dir()
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / f"{instance_dir.name}.pg.log"
        if log.is_file():
            shutil.copy2(log, dest)
        else:
            dest.write_text(f"{NO_PG_LOG_NOTE}\n")
        return dest
    except OSError:
        logger.warning(f"could not preserve failed fixture pg.log into {dest_dir}")
        return None


_PG_START_ATTEMPTS = 3


def _is_port_bind_failure(exc: subprocess.CalledProcessError, log: Path) -> bool:
    return "Address already in use" in f"{exc.stderr or ''}\n{_read_log_tail(log)}"


def teardown_throwaway(
    tmp: Path,
    data: Path,
    registration: Registration,
    process: subprocess.Popen[bytes] | None = None,
    *,
    home: Path,
    foreground: bool = False,
    bin_dir: Path | None = None,
) -> None:
    if process is not None:
        stop_foreground_postgres(process)
    elif not foreground:
        subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + static flags
            [
                pg_tool("pg_ctl", bin_dir=bin_dir, home=home),
                "-D",
                str(data),
                "-m",
                "immediate",
                "stop",
            ],
            check=False,
            capture_output=True,
        )
    shutil.rmtree(tmp, ignore_errors=True)
    unregister_throwaway(registration)


@contextmanager
def throwaway_postgres(
    schema_sql: str | None = None,
    *,
    config: ThrowawayConfig,
    base: Path | None = None,
    foreground: bool = False,
    watch_stalls: bool = True,
    readiness_timeout_s: float = 60,
) -> Generator[str]:
    import psycopg

    sweep_orphaned_throwaway_clusters(config=config)
    tmp = data = Path()
    port = 0
    process: subprocess.Popen[bytes] | None = None
    attempt = 0
    candidates = iter(config.candidate_ports)
    while True:
        attempt += 1
        tmp = Path(
            tempfile.mkdtemp(
                prefix=THROWAWAY_PREFIX,
                dir=base
                or _throwaway_base.select_throwaway_base(
                    override=config.override, default_root=config.default_root
                ),
            )
        )
        data = tmp / "data"
        log = tmp / "pg.log"
        port, registration = allocate_port(tmp, config=config, candidates=candidates)
        try:
            try:
                subprocess.run(  # noqa: S603 — argv is the resolved initdb path + static flags
                    [
                        pg_tool("initdb", bin_dir=config.bin_dir, home=config.home),
                        "-D",
                        str(data),
                        "-U",
                        "meiva",
                        "-A",
                        "trust",
                        "--no-sync",
                        "--encoding=UTF8",
                        "--locale=C",
                    ],
                    check=True,
                    capture_output=True,
                )
                if foreground:
                    process = start_foreground_postgres(
                        [
                            str(pg_tool("postgres", bin_dir=config.bin_dir, home=config.home)),
                            "-D",
                            str(data),
                            "-p",
                            str(port),
                            "-c",
                            "listen_addresses=127.0.0.1",
                            "-c",
                            f"unix_socket_directories={tmp}",
                            "-c",
                            "fsync=off",
                            "-c",
                            "full_page_writes=off",
                            "-c",
                            "synchronous_commit=off",
                            *shlex.split(f"{pg_tz_args()} {pg_shm_args()}"),
                        ],
                        log=log,
                        env=pg_start_env(),
                    )
                    wait_foreground_postgres(
                        process, log=log, port=port, data=data, timeout_s=readiness_timeout_s
                    )
                else:
                    subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + static flags
                        [
                            pg_tool("pg_ctl", bin_dir=config.bin_dir, home=config.home),
                            "-D",
                            str(data),
                            "-l",
                            str(log),
                            "-w",
                            "-t",
                            "60",
                            "start",
                            "-o",
                            f"-p {port} -c listen_addresses=127.0.0.1 "
                            f"-c unix_socket_directories={tmp} "
                            "-c fsync=off -c full_page_writes=off -c synchronous_commit=off "
                            f"{pg_tz_args()} {pg_shm_args()}",
                        ],
                        check=True,
                        capture_output=True,
                        env=pg_start_env(),
                    )
            except subprocess.CalledProcessError as exc:
                if _is_port_bind_failure(exc, log) and attempt < _PG_START_ATTEMPTS:
                    teardown_throwaway(
                        tmp,
                        data,
                        registration,
                        process,
                        foreground=foreground,
                        bin_dir=config.bin_dir,
                        home=config.home,
                    )
                    process = None
                    continue
                preserved = _preserve_failed_fixture_log(tmp, artifact_dir=config.artifact_dir)
                raise ThrowawayPgStartError(exc, log, preserved) from exc
            break
        except BaseException:
            teardown_throwaway(
                tmp,
                data,
                registration,
                process,
                foreground=foreground,
                bin_dir=config.bin_dir,
                home=config.home,
            )
            raise

    admin = f"postgresql://meiva@127.0.0.1:{port}/postgres"
    url = f"postgresql://meiva@127.0.0.1:{port}/meiva_citest"

    try:
        with psycopg.connect(admin, autocommit=True) as conn, conn.cursor() as cur:
            cur.execute("CREATE ROLE meiva_citest LOGIN SUPERUSER")
            cur.execute("CREATE DATABASE meiva_citest")

        if schema_sql is not None:
            from langgraph.checkpoint.postgres import PostgresSaver

            with psycopg.connect(url, autocommit=True) as conn, conn.cursor() as cur:
                cur.execute(schema_sql)  # type: ignore[arg-type]
            with PostgresSaver.from_conn_string(url) as saver:
                saver.setup()

        with (
            stall_guard(admin, port, config=StallWatchdogConfig())
            if watch_stalls
            else contextlib.nullcontext()
        ):
            yield url
    finally:
        teardown_throwaway(
            tmp,
            data,
            registration,
            process,
            foreground=foreground,
            bin_dir=config.bin_dir,
            home=config.home,
        )
