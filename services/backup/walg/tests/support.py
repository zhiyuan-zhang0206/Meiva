"""Shared test scaffolding for the WAL-G package: a sandbox home and a real Postgres.

Not a test module: pytest never collects this file. The sandbox installs
`fake_walg.sh` where the pinned binary lives, writes a valid configuration and
key, and points the settings and the binary pin at them, so production code runs
unchanged. The home path deliberately holds a space and a `%p`: the archive
command must survive both (an unescaped `%p` in a path would be expanded by
Postgres into a segment path).
"""

from __future__ import annotations

import json
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from collections.abc import Generator
from contextlib import AbstractContextManager, contextmanager, suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import psycopg
import pytest

from base.cluster import postgres as native_postgres
from base.cluster.dataplane import pg_admin, walg_binary
from base.cluster.dataplane.pg_tools import pg_shm_args, pg_start_env, pg_tool, pg_tz_args
from base.host.env import port_lease
from base.paths import meiva_home
from base.paths.tests.homes import point_home
from services.backup.walg.archive import archive_pg_args
from services.backup.walg.pg_target import PgTarget

FAKE_WALG = Path(__file__).with_name("fake_walg.sh")
FIXTURES = Path(__file__).with_name("fixtures")

# Values that must never appear in argv, postgresql.conf, a log line or an error.
ACCESS_KEY_ID = "LTAI-test-access-key-id"
ACCESS_KEY_SECRET = "test-access-key-secret-0123456789"  # noqa: S105 — a fixture value
KEY_HEX = "ab" * 32
PREFIX = "oss://meiva-backups/meiva-walg/test-home/pg17/gen1/"
SECRETS = (ACCESS_KEY_ID, ACCESS_KEY_SECRET, KEY_HEX)


@dataclass
class Sandbox:
    home: Path
    store_dir: Path
    config_file: Path
    key_file: Path
    enabled: bool = True

    # meiva:note
    # The configured WAL-G path as this sandbox holds it: its file, or None while off.
    @property
    def path(self) -> Path | None:
        return self.config_file if self.enabled else None

    def config(self) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(self.config_file.read_text())
        return payload

    def write_config(self, payload: dict[str, Any]) -> None:
        self.config_file.write_text(json.dumps(payload))
        self.config_file.chmod(0o600)

    def set_mode(self, mode: str) -> None:
        (self.store_dir / "mode").write_text(mode)

    def put(self, name: str, text: str) -> None:
        """Script the fake: write one of the files it reads from its directory."""
        (self.store_dir / name).write_text(text)

    def fail(self, *commands: str) -> None:
        """Make these wal-g commands exit 1 (the rest keep working)."""
        self.put("fail-commands", " ".join(commands))

    def env_log(self) -> list[str]:
        log = self.store_dir / "env.log"
        return log.read_text().splitlines() if log.exists() else []

    def verify_env_log(self) -> list[str]:
        log = self.store_dir / "verify-env.log"
        return log.read_text().splitlines() if log.exists() else []

    def calls(self) -> list[str]:
        log = self.store_dir / "calls.log"
        return log.read_text().splitlines() if log.exists() else []

    def stored(self) -> list[str]:
        root = self.store_dir / "store"
        return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


def fixture_text(name: str) -> str:
    """A file of real WAL-G output, kept in `tests/fixtures/` (see the test that uses it)."""
    return (FIXTURES / name).read_text()


def valid_config(key_file: Path, **overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "WALG_OSS_PREFIX": PREFIX,
        "OSS_ACCESS_KEY_ID": ACCESS_KEY_ID,
        "OSS_ACCESS_KEY_SECRET": ACCESS_KEY_SECRET,
        "OSS_ENDPOINT": "https://oss-cn-shanghai.aliyuncs.com",
        "OSS_REGION": "cn-shanghai",
        "WALG_LIBSODIUM_KEY_PATH": str(key_file),
        "WALG_LIBSODIUM_KEY_TRANSFORM": "hex",
        "WALG_PREVENT_WAL_OVERWRITE": "true",
    }
    payload.update(overrides)
    return {key: value for key, value in payload.items() if value is not None}


def make_sandbox(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True
) -> Sandbox:
    home = tmp_path / "meiva home%p"
    store_dir = home / "secrets" / "walg"
    store_dir.mkdir(parents=True)
    key_file = store_dir / "libsodium.key"
    key_file.write_text(KEY_HEX + "\n")
    key_file.chmod(0o600)
    config_file = store_dir / "walg.json"

    binary = home / "runtime" / "walg" / "wal-g"
    binary.parent.mkdir(parents=True)
    shutil.copy(FAKE_WALG, binary)
    binary.chmod(0o755)

    point_home(monkeypatch, home)
    # The fake is not the pinned build; everything else (path, argv, config) is real.
    monkeypatch.setattr(walg_binary, "installed_problem", lambda: None)
    sandbox = Sandbox(home, store_dir, config_file, key_file, enabled)
    sandbox.write_config(valid_config(key_file))
    if enabled:
        (store_dir / "enabled").write_text("1")
    return sandbox


# ── a real Postgres, launched the way the cluster launches it ────────────────


@dataclass
class PgInstance:
    root: Path
    data: Path
    port: int
    socket_dir: Path

    def connect(self) -> psycopg.Connection[Any]:
        return psycopg.connect(
            host=str(self.socket_dir),
            port=self.port,
            user="meiva",
            dbname="postgres",
            autocommit=True,
        )

    # meiva:note
    # This instance as a home's locally owned Postgres: its owner-only admin socket and data
    # directory, the pair the production custody check binds a connection to.
    @property
    def target(self) -> PgTarget:
        url = f"postgresql://meiva@/postgres?host={self.socket_dir}&port={self.port}"
        return PgTarget(url, self.data, "postgres")


# meiva:note
# One port this session holds, from the host-wide lease: the number is refused to
# every other live holder on the host, so two workers — or two runs of one shard on
# one machine — cannot be given it.
def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


# meiva:note
# initdb + launch a Postgres laid out like a home's: `<root>/data` listening on the
# canonical owner-only socket directory of the home `<root>`, so the production custody
# check accepts it. With a `archive` sandbox it carries that sandbox's archive settings.
#
# The socket directory is short (`/tmp`), the data is disposable (fsync off), and
# shutdown is immediate: a test never waits for a hung archive command.
@contextmanager
def postgres_instance(*extra_args: str, path: Path | None = None) -> Generator[PgInstance]:
    root = Path(tempfile.mkdtemp(prefix="meiva-walgpg-", dir="/tmp"))
    data = root / "data"
    socket_dir = pg_admin.pg_socket_dir(home=root)
    instance = PgInstance(root=root, data=data, port=_free_port(), socket_dir=socket_dir)
    process: subprocess.Popen[bytes] | None = None
    try:
        # meiva:note
        # The argv is the resolved initdb path plus static flags.
        subprocess.run(  # noqa: S603
            [
                str(pg_tool("initdb")),
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
        archive_args = archive_pg_args(path=path)
        argv = [
            str(pg_tool("postgres")),
            "-D",
            str(data),
            "-p",
            str(instance.port),
            "-c",
            "listen_addresses=127.0.0.1",
            "-c",
            f"unix_socket_directories={socket_dir}",
            "-c",
            "fsync=off",
            "-c",
            "full_page_writes=off",
            "-c",
            "synchronous_commit=off",
            *shlex.split(pg_tz_args()),
            *shlex.split(pg_shm_args()),
            *archive_args,
            *extra_args,
        ]
        # meiva:note
        # Launched by the native starter, so the postmaster carries the launch receipt the
        # production custody check demands of an admin connection.
        children: list[subprocess.Popen[bytes]] = []

        def ready() -> bool:
            try:
                instance.connect().close()
            except psycopg.OperationalError:
                return False
            return True

        native_postgres.start(
            data, instance.port, argv, pg_start_env(), ready=ready, retained_children=children
        )
        process = children[0]
        yield instance
    finally:
        if process is not None and process.poll() is None:
            process.send_signal(signal.SIGQUIT)
            with suppress(subprocess.TimeoutExpired):
                process.wait(timeout=30)
            if process.poll() is None:
                process.kill()
                process.wait()
        shutil.rmtree(socket_dir, ignore_errors=True)
        shutil.rmtree(root, ignore_errors=True)


def _ambient_sandbox_path() -> Path | None:
    store = meiva_home() / "secrets" / "walg"
    return store / "walg.json" if (store / "enabled").exists() else None


# meiva:note
# `postgres_instance` carrying the sandbox's `archive_pg_args()` on its command line.
#
# Without a `sandbox` the archive settings come from the active sandbox in `meiva_home()`.
def archiving_postgres(
    *extra_args: str, sandbox: Sandbox | None = None
) -> AbstractContextManager[PgInstance]:
    path = sandbox.path if sandbox is not None else _ambient_sandbox_path()
    return postgres_instance(*extra_args, path=path)


def wait_for(predicate: Any, *, seconds: float = 60, what: str) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out after {seconds:g}s waiting for {what}")


def write_some_wal_and_switch(conn: psycopg.Connection[Any]) -> None:
    """Make sure the current segment has content, then close it so it becomes archivable."""
    conn.execute("CREATE TABLE IF NOT EXISTS walg_probe (n int)")
    conn.execute("INSERT INTO walg_probe SELECT generate_series(1, 100)")
    conn.execute("SELECT pg_switch_wal()")


def archive_current_segment(conn: psycopg.Connection[Any], sandbox: Sandbox) -> str:
    """Close the current WAL segment and wait until the fake store holds it; returns its name.

    A segment that has had no WAL since it started is not switched, so one record is
    emitted first.
    """
    conn.execute("SELECT pg_logical_emit_message(false, 'walg-test', 'x')")
    row = conn.execute("SELECT pg_walfile_name(pg_current_wal_lsn())").fetchone()
    assert row is not None
    segment = str(row[0])
    conn.execute("SELECT pg_switch_wal()")
    wait_for(lambda: segment in sandbox.stored(), what=f"{segment} in the store")
    return segment


def take_basebackup(sandbox: Sandbox, pg: PgInstance, name: str) -> str:
    """A base backup of `pg` as the fake's `backup-fetch` serves it (no WAL inside, like WAL-G's)."""
    destination = sandbox.store_dir / "basebackups" / name
    subprocess.run(  # noqa: S603 — the resolved pg_basebackup with static flags
        [
            str(pg_tool("pg_basebackup")),
            "-D",
            str(destination),
            "-h",
            str(pg.socket_dir),
            "-p",
            str(pg.port),
            "-U",
            "meiva",
            "-X",
            "none",
            "--checkpoint=fast",
            "--no-sync",
        ],
        check=True,
        capture_output=True,
        timeout=120,
    )
    return name


# meiva:note
# A dial to Postgres that must not be entered: entering it fails the test.
class NeverDialed:
    def __enter__(self) -> psycopg.Connection[Any]:
        raise AssertionError("Postgres was dialed")

    def __exit__(self, *exc: object) -> None:
        return None


# meiva:note
# A home's Postgres that is not there: its owner-only socket directory does not exist.
def refused_target() -> PgTarget:
    return PgTarget(
        "postgresql://meiva@/postgres?host=/nonexistent&port=1", Path("/nonexistent"), "postgres"
    )
