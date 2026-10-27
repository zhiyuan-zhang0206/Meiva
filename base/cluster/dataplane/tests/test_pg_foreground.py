"""Foreground restore databases remain owned and cannot escape shutdown."""

from __future__ import annotations

import contextlib
import json
import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from contextlib import nullcontext
from pathlib import Path

import psutil
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from base.cluster.dataplane import pg_foreground, pg_plan, pg_tools
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig
from base.paths import meiva_home


@pytest.fixture
def foreground_root(postgres_access: None) -> Iterator[Path]:
    """Use short socket paths and restrict every sweep to this test's instances."""
    with tempfile.TemporaryDirectory(prefix="meiva-fg-", dir="/tmp") as directory:
        root = Path(directory)
        try:
            yield root
        finally:
            # Independent cleanup also runs when an assertion inside the context fails.
            for data in root.glob("meiva-pg-*/data"):
                if (data / "PG_VERSION").is_file():
                    subprocess.run(  # noqa: S603 -- only this test's own temporary PGDATA
                        [
                            pg_tools.pg_tool("pg_ctl", home=meiva_home()),
                            "-D",
                            str(data),
                            "-m",
                            "immediate",
                            "-t",
                            "2",
                            "stop",
                        ],
                        capture_output=True,
                        check=False,
                        timeout=5,
                    )


@pytest.mark.parametrize("interrupted", [False, True])
def test_native_foreground_is_direct_child_and_reaped_on_context_exit(
    foreground_root: Path, interrupted: bool
) -> None:
    """Observe the real postmaster, including the BaseException cleanup path."""
    pid = 0
    instance = foreground_root
    outcome = pytest.raises(KeyboardInterrupt) if interrupted else nullcontext()
    config = ThrowawayConfig(foreground_root, default_root=foreground_root, home=meiva_home())
    with (
        outcome,
        pg_tools.throwaway_postgres(base=foreground_root, foreground=True, config=config) as url,
    ):
        with psycopg.connect(url) as connection:
            row = connection.execute("SHOW data_directory").fetchone()
            assert row is not None
            data = Path(row[0])
            instance = data.parent
            pid = int((data / "postmaster.pid").read_text().splitlines()[0])
            assert psutil.Process(pid).ppid() == os.getpid()
            assert os.getpgid(pid) == os.getpgrp()
            assert connection.execute("SELECT 1").fetchone() == (1,)
            assert connection.execute("SHOW timezone").fetchone() == ("UTC",)
            assert connection.execute("SHOW shared_memory_type").fetchone() == ("mmap",)
            assert connection.execute("SHOW dynamic_shared_memory_type").fetchone() == ("mmap",)
        if interrupted:
            raise KeyboardInterrupt

    assert pid > 0
    assert not instance.exists()
    assert not psutil.pid_exists(pid)
    with pytest.raises(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    assert not list(foreground_root.glob("meiva-pg-*"))


def test_startup_failure_reaps_the_native_postmaster(
    tmp_path: Path,
    postgres_access: None,
) -> None:
    """An invalid native invocation must fail with its direct child already reaped."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        unused_port = int(sock.getsockname()[1])

    log = tmp_path / "pg.log"
    process = pg_foreground.start_foreground_postgres(
        [str(pg_tools.pg_tool("postgres", home=meiva_home())), "--meiva-invalid-start-option"],
        log=log,
        env=pg_tools.pg_start_env(),
    )
    try:
        with pytest.raises(RuntimeError, match="foreground Postgres exited"):
            pg_foreground.wait_foreground_postgres(
                process,
                data=tmp_path / "data",
                log=log,
                port=unused_port,
                timeout_s=5,
            )
        assert process.returncode is not None and process.returncode != 0
        with pytest.raises(ChildProcessError):
            os.waitpid(process.pid, os.WNOHANG)
    finally:
        pg_foreground.stop_foreground_postgres(process)


def test_readiness_rejects_an_unrelated_postgres_on_the_selected_port(
    tmp_path: Path, db_url: str
) -> None:
    """A port collision must never authorize creating roles in the other server."""
    with pytest.raises(RuntimeError, match="another data directory"):
        pg_plan.verify_foreground_data_dir(str(tmp_path / "other-db"), tmp_path / "owned-db")

    bin_dir = _write_stub_pg_bindir(tmp_path / "bin")
    port = int(str(conninfo_to_dict(db_url)["port"]))
    log = tmp_path / "pg.log"
    process = pg_foreground.start_foreground_postgres(
        [str(bin_dir / "postgres"), "-D", str(tmp_path / "owned-db")],
        log=log,
        env=pg_tools.pg_start_env(),
    )
    try:
        with pytest.raises(RuntimeError, match="another data directory"):
            pg_foreground.wait_foreground_postgres(
                process, data=tmp_path / "owned-db", log=log, port=port
            )
    finally:
        process.terminate()
        process.wait(timeout=5)


def test_readiness_timeout_is_bounded(tmp_path: Path) -> None:
    bin_dir = _write_stub_pg_bindir(tmp_path / "bin")
    log = tmp_path / "pg.log"
    process = pg_foreground.start_foreground_postgres(
        [str(bin_dir / "postgres"), "-D", str(tmp_path / "data")],
        log=log,
        env=pg_tools.pg_start_env(),
    )
    try:
        with pytest.raises(TimeoutError, match="did not become ready"):
            pg_foreground.wait_foreground_postgres(
                process,
                data=tmp_path / "data",
                log=log,
                port=1,
                timeout_s=0,
            )
    finally:
        process.terminate()
        process.wait(timeout=5)


def _write_stub_pg_bindir(
    bin_dir: Path,
    *,
    stop_kills_child: bool = True,
    env_dump: Path | None = None,
) -> Path:
    bin_dir.mkdir(parents=True, exist_ok=True)
    initdb = bin_dir / "initdb"
    initdb.write_text(
        "#!/bin/sh\n"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "-D" ]; then\n'
        '    mkdir -p "$2"\n'
        '    echo "17" > "$2/PG_VERSION"\n'
        "    exit 0\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "exit 1\n"
    )
    initdb.chmod(0o755)

    dump_snippet = ""
    if env_dump is not None:
        dumper = bin_dir / "dump_env.py"
        dumper.write_text(
            "import json, os, pathlib, sys\n"
            "pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(os.environ)))\n"
        )
        dump_snippet = (
            f"{shlex.quote(sys.executable)} {shlex.quote(str(dumper))} "
            f"{shlex.quote(str(env_dump))}\n"
        )
    postgres = bin_dir / "postgres"
    postgres.write_text(
        "#!/bin/sh\n"
        f"{dump_snippet}"
        "while [ $# -gt 0 ]; do\n"
        '  if [ "$1" = "-D" ]; then\n'
        '    echo "$$" > "$2/postmaster.pid"\n'
        "    break\n"
        "  fi\n"
        "  shift\n"
        "done\n"
        "exec sleep 60\n"
    )
    postgres.chmod(0o755)

    pg_ctl = bin_dir / "pg_ctl"
    if stop_kills_child:
        pg_ctl.write_text(
            "#!/bin/sh\n"
            "while [ $# -gt 0 ]; do\n"
            '  if [ "$1" = "-D" ] && [ -f "$2/postmaster.pid" ]; then\n'
            '    kill -9 "$(cat "$2/postmaster.pid")" 2>/dev/null || true\n'
            "    exit 0\n"
            "  fi\n"
            "  shift\n"
            "done\n"
            "exit 0\n"
        )
    else:
        pg_ctl.write_text("#!/bin/sh\nexit 0\n")
    pg_ctl.chmod(0o755)
    return bin_dir


def test_unreapable_foreground_retains_data_and_registration(
    tmp_path: Path,
) -> None:
    bin_dir = _write_stub_pg_bindir(tmp_path / "bin", stop_kills_child=False)
    instance = tmp_path / "meiva-pg-owned"
    data = instance / "data"
    data.mkdir(parents=True)
    sentinel = data / "retained"
    sentinel.write_text("must survive unconfirmed shutdown")
    registration = pg_tools.register_throwaway(instance, 5559)
    process = pg_foreground.start_foreground_postgres(
        [str(bin_dir / "postgres"), "-D", str(data)],
        log=instance / "pg.log",
        env=pg_tools.pg_start_env(),
    )
    try:
        with pytest.raises(subprocess.TimeoutExpired):
            pg_tools.teardown_throwaway(
                instance,
                data,
                registration,
                process,
                foreground=True,
                bin_dir=bin_dir,
                home=meiva_home(),
            )

        assert sentinel.read_text() == "must survive unconfirmed shutdown"
        assert registration.lock.is_file()
    finally:
        process.terminate()
        process.wait(timeout=5)
        pg_tools.unregister_throwaway(registration)


@pytest.mark.parametrize("stop_fails", [False, True])
def test_startup_timeout_keeps_the_child_handle_for_cleanup(
    foreground_root: Path, tmp_path: Path, stop_fails: bool
) -> None:
    """Even readiness failure must retain live PGDATA until its owner can reap it."""
    bin_dir = _write_stub_pg_bindir(tmp_path / "bin", stop_kills_child=not stop_fails)
    config = ThrowawayConfig(
        foreground_root,
        default_root=foreground_root,
        artifact_dir=tmp_path / "artifacts",
        bin_dir=bin_dir,
        home=meiva_home(),
    )
    error = subprocess.TimeoutExpired if stop_fails else TimeoutError
    try:
        with (
            pytest.raises(error),
            pg_tools.throwaway_postgres(
                base=foreground_root,
                foreground=True,
                config=config,
                readiness_timeout_s=0.2,
            ),
        ):
            pytest.fail("a failed readiness check must never yield a database URL")
        if stop_fails:
            [instance] = list(foreground_root.glob("meiva-pg-*"))
            assert (instance / "data" / "PG_VERSION").is_file()
            assert (instance / "owner.lock").is_file()
        else:
            assert not list(foreground_root.glob("meiva-pg-*"))
    finally:
        for instance in foreground_root.glob("meiva-pg-*"):
            pid_file = instance / "data" / "postmaster.pid"
            if pid_file.is_file():
                pid = int(pid_file.read_text().strip())
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGKILL)
                with contextlib.suppress(ChildProcessError):
                    os.waitpid(pid, 0)
            shutil.rmtree(instance, ignore_errors=True)


def test_foreground_start_is_handed_the_built_start_env(
    foreground_root: Path, tmp_path: Path
) -> None:
    """Task #3754: the direct `postgres` spawn receives the same explicit start
    env as the pg_ctl path (see pg_tools.pg_start_env) — the postmaster is
    never started without a locale."""
    env_dump = tmp_path / "env.json"
    bin_dir = _write_stub_pg_bindir(tmp_path / "bin", stop_kills_child=True, env_dump=env_dump)
    config = ThrowawayConfig(
        foreground_root,
        default_root=foreground_root,
        artifact_dir=tmp_path / "artifacts",
        bin_dir=bin_dir,
        home=meiva_home(),
    )

    with (
        pytest.raises(TimeoutError),
        pg_tools.throwaway_postgres(
            base=foreground_root,
            foreground=True,
            config=config,
            readiness_timeout_s=0.2,
        ),
    ):
        pytest.fail("readiness never succeeds in this test")

    spawned_env = json.loads(env_dump.read_text())
    expected_env = pg_tools.pg_start_env()
    for key, value in expected_env.items():
        assert spawned_env.get(key) == value
    assert bool(spawned_env.get("LC_ALL") or spawned_env.get("LANG"))
