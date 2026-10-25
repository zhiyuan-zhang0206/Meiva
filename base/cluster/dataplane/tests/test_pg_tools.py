"""mmap-backed shared memory pinning for every PG Meiva starts (Task #1263), and
the Postgres session timezone pin (tz audit PR-1).

`pg_shm_args` / `pg_tz_args` feed the `pg_ctl -o` string of both startup paths
— the per-cluster data plane (`cli/commands/data_plane/cluster_instance.py`) and the
throwaway test/eval clusters (`throwaway_postgres`). `pg_shm_args`' two
settings move Postgres' main shared memory region and its dynamic segments out
of POSIX shm (/dev/shm on Linux) into files under the data directory, so an
external unlink of /dev/shm cannot take a running instance down — the staging
incident that motivated the task (the machine-side fix was the same two
settings). `pg_tz_args` pins the session timezone to UTC so psycopg3 returns
every timestamptz as a stable `+00:00`-offset datetime instead of one that
drifts with the host OS timezone.

`pg_start_env` (Task #3754) is the third startup-path fragment: the child
environment for a server start, the operator's process mechanics only (no
credential the caller holds), plus the macOS locale fallback the postmaster
needs when the caller's environment carries none (a launchd job or a
non-interactive ssh session).
"""

import json
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from base.cluster.dataplane import pg_plan, pg_tools
from base.cluster.dataplane import pg_stall_watchdog as guard
from base.cluster.dataplane.pg_stall_watchdog import StallWatchdogConfig
from base.cluster.dataplane.pg_throwaway_base import ThrowawayConfig

_MMAP_ARGS = "-c shared_memory_type=mmap -c dynamic_shared_memory_type=mmap"


# meiva:note
# The probe a module-level `skipif` is allowed to call. `pg_tool` raises when the box has no
# installation; evaluated at import, that raise turned "no PostgreSQL here" into a collection
# error that stopped the whole tree on a macOS runner (task #5531's darwin lane). The pair
# below pins both halves: a lookup that raises answers False, and one that finds the binary
# answers True - so a skip predicate written with this probe still runs the test where the tool
# exists.
def test_has_pg_tool_answers_false_instead_of_raising(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    assert (
        pg_tools.has_pg_tool(
            "pg_dump",
            bin_dir=missing,
            home=tmp_path,
            platform_bin_dir=missing,
            path_env="",
        )
        is False
    )


def test_has_pg_tool_answers_true_for_a_tool_it_can_find(tmp_path: Path) -> None:
    found = tmp_path / "pg_dump"
    found.write_text("")

    assert pg_tools.has_pg_tool("pg_dump", bin_dir=tmp_path) is True


def test_pg_shm_args_linux_branch() -> None:
    """Linux — the incident platform: the pin is unconditional there."""
    assert pg_plan.build_pg_shm_args(macos=False) == _MMAP_ARGS


def test_pg_shm_args_macos_branch() -> None:
    """macOS — compatibility verified 2026-08-13: the vendored PG 17.4 starts
    with both settings and they take effect (pg_settings, source=command line);
    mmap is already the macOS default for the main region and DSM mmap has been
    supported since PG 15. So macOS pins them too."""
    assert pg_plan.build_pg_shm_args(macos=True) == _MMAP_ARGS


def test_pg_shm_args_macos_incompatibility_keeps_status_quo() -> None:
    """The task's named fallback: a future macOS PG build that rejects either
    option flips `PG_SHM_MMAP_OK_ON_MACOS` to False — macOS then keeps its
    status quo (no explicit settings) while Linux keeps the pin."""
    assert pg_plan.build_pg_shm_args(macos=True, mmap_ok_on_macos=False) == ""
    assert pg_plan.build_pg_shm_args(macos=False, mmap_ok_on_macos=False) == _MMAP_ARGS


def test_pg_tz_args_is_unconditional_utc() -> None:
    """Unlike `pg_shm_args`, there is no platform branch: every PG this
    codebase starts pins the session timezone to UTC."""
    assert pg_tools.pg_tz_args() == "-c timezone=UTC"


def test_throwaway_pg_session_timezone_is_utc(db_conn: psycopg.Connection) -> None:
    """The live throwaway pg the test suite runs against (started via
    `throwaway_postgres`, not `cluster_instance.py`) actually carries the
    pin — this is the seam PR-1's behavior-change test lock depends on:
    without it, `.isoformat()` on a value read back through `db_conn` would
    carry the CI/dev host's OS timezone offset instead of a stable `+00:00`."""
    with db_conn.cursor() as cur:
        cur.execute("SHOW timezone")
        row = cur.fetchone()
    assert row is not None
    assert row[0] == "UTC"


@pytest.fixture
def scratch_pg_root(postgres_access: None) -> Iterator[Path]:
    """A short scratch throwaway base for tests that really start a server.

    The Postgres socket path (`.s.PGSQL.<port>`) is capped at 103 bytes, so the
    instance root must be short; teardown force-stops and deletes anything left
    under it."""
    root = Path(tempfile.mkdtemp(prefix="meiva-pg-env-", dir="/tmp"))
    yield root
    for data in root.glob("meiva-pg-*/data"):
        if (data / "PG_VERSION").is_file():
            subprocess.run(  # noqa: S603 — argv is the resolved pg_ctl path + this test's own dir
                [pg_tools.pg_tool("pg_ctl"), "-D", str(data), "-m", "immediate", "stop"],
                check=False,
                capture_output=True,
            )
    shutil.rmtree(root, ignore_errors=True)


def test_pg_start_env_macos_fills_lc_all_when_missing() -> None:
    """Task #3754: launchd and non-interactive ssh start PG with no locale at
    all, and the Homebrew postmaster then aborts ("postmaster became
    multithreaded during startup") — so the start env supplies one. Verified
    live on macOS 2026-09-17 against postgresql@17 (17.11)."""
    raw_env = {"PATH": os.environ["PATH"]}
    env = pg_plan.build_pg_start_env(raw_env, macos=True)

    assert env["LC_ALL"] == "en_US.UTF-8"
    assert env["PATH"] == os.environ["PATH"], "the operator's PATH crosses"


def test_pg_start_env_macos_keeps_a_caller_locale() -> None:
    """A caller that expresses a locale keeps it verbatim — the fallback never
    overrides a choice (LC_ALL directly, or LANG when LC_ALL is unset)."""
    with_lc_all = {"PATH": "/usr/bin", "LC_ALL": "de_DE.UTF-8"}
    assert pg_plan.build_pg_start_env(with_lc_all, macos=True)["LC_ALL"] == "de_DE.UTF-8"

    with_lang = {"PATH": "/usr/bin", "LANG": "de_DE.UTF-8"}
    assert "LC_ALL" not in pg_plan.build_pg_start_env(with_lang, macos=True)


def test_pg_start_env_macos_lc_ctype_alone_is_not_a_locale() -> None:
    """LC_CTYPE alone (an ssh session forwarding only LC_CTYPE) does not keep
    the postmaster off the CoreFoundation path — verified: it still aborts, so
    LC_CTYPE must not suppress the fallback."""
    raw_env = {"PATH": "/usr/bin", "LC_CTYPE": "UTF-8"}
    assert pg_plan.build_pg_start_env(raw_env, macos=True)["LC_ALL"] == "en_US.UTF-8"


def test_pg_start_env_macos_empty_value_counts_as_missing() -> None:
    """setlocale ignores an empty LC_ALL (verified: the postmaster still
    aborts), so the fallback treats it exactly like unset."""
    raw_env = {"PATH": "/usr/bin", "LC_ALL": ""}
    assert pg_plan.build_pg_start_env(raw_env, macos=True)["LC_ALL"] == "en_US.UTF-8"


def test_pg_start_env_non_macos_adds_no_locale() -> None:
    """Linux needs nothing: an absent locale resolves to C without threads, and
    a minimal image may not have en_US.UTF-8 generated."""
    raw_env = {"PATH": "/usr/bin", "HOME": "/Users/test"}
    assert pg_plan.build_pg_start_env(raw_env, macos=False) == raw_env


def test_pg_start_env_carries_only_process_mechanics() -> None:
    """The postmaster keeps the operator's PATH, home, user, temp dir, timezone
    and locale, never configuration or a credential the caller holds."""
    # The raw process environment the spawning `meiva start` may hold (Settings
    # is not involved): delivered credentials, `.env` secrets, libpq leftovers.
    raw_env = {
        "PATH": os.environ["PATH"],
        "MEIVA_API_TOKEN": "gateway-api-token-" + "t" * 32,
        "MEIVA_CLUSTER_SECRET": "human-" + "h" * 40,
        "PGPASSWORD": "inherited",
        "TZ": "Asia/Shanghai",
        "LC_CTYPE": "UTF-8",
    }

    env = pg_plan.build_pg_start_env(raw_env, macos=False)

    mechanics = {"PATH", "HOME", "USER", "LOGNAME", "TMPDIR", "TZ", "LANG"} | {
        "SYSTEMROOT",
        "WINDIR",
        "SYSTEMDRIVE",
        "COMSPEC",
        "PATHEXT",
        "TEMP",
        "TMP",
        "USERPROFILE",
    }
    assert set(env) <= mechanics | {name for name in env if name.startswith("LC_")}
    assert env["TZ"] == "Asia/Shanghai" and env["LC_CTYPE"] == "UTF-8"
    assert env["PATH"] == os.environ["PATH"]


def test_throwaway_pg_ctl_start_is_handed_the_built_start_env(
    scratch_pg_root: Path, tmp_path: Path
) -> None:
    """Task #3754: the fixture's `pg_ctl start` receives pg_start_env() — the
    postmaster environment is built explicitly instead of inherited from
    whatever process runs the suite."""
    real_pg_ctl = pg_tools.pg_tool("pg_ctl")
    env_dump = tmp_path / "pg_ctl_env.json"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    dumper = bin_dir / "dump_env.py"
    dumper.write_text(
        "import json, os, pathlib, sys\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps(dict(os.environ)))\n"
    )
    wrapper = bin_dir / "pg_ctl"
    wrapper.write_text(
        "#!/bin/sh\n"
        'for arg in "$@"; do\n'
        '  if [ "$arg" = "start" ]; then\n'
        f"    {shlex.quote(sys.executable)} {shlex.quote(str(dumper))} "
        f"{shlex.quote(str(env_dump))}\n"
        "  fi\n"
        "done\n"
        f'exec {shlex.quote(str(real_pg_ctl))} "$@"\n'
    )
    wrapper.chmod(0o755)
    config = ThrowawayConfig(None, default_root=scratch_pg_root, bin_dir=bin_dir)

    with (
        pg_tools.throwaway_postgres(config=config) as url,
        psycopg.connect(url) as conn,
    ):
        assert conn.execute("select 1").fetchone() == (1,)

    recorded = json.loads(env_dump.read_text())
    expected = pg_tools.pg_start_env()
    for key, value in expected.items():
        assert recorded.get(key) == value


@pytest.mark.parametrize("field", ["probe_interval_s", "probe_timeout_s"])
@pytest.mark.parametrize("value", [0, -1.0])
def test_stall_watchdog_config_rejects_a_cadence_that_would_spin_or_never_time_out(
    field: str, value: float
) -> None:
    overrides: dict[str, Any] = {field: value}
    with pytest.raises(ValueError, match=field):
        StallWatchdogConfig(**overrides)


def test_stall_guard_joins_a_real_probe_before_releasing_access(
    db_url: str,
) -> None:
    stall_config = StallWatchdogConfig(probe_interval_s=0.01)
    port = conninfo_to_dict(db_url)["port"]
    assert isinstance(port, str)
    with guard.stall_guard(db_url, int(port), config=stall_config) as watchdog:
        assert watchdog.probed.wait(3), "the owned watchdog must actually query its database"
    assert not watchdog.is_alive(), "the DB access window must outlive its probe thread"


def test_stall_guard_reports_a_probe_that_does_not_join(
    postgres_access: None,
) -> None:
    entered, release = threading.Event(), threading.Event()
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(4)
    port = int(server.getsockname()[1])

    def hold_handshake() -> None:
        try:
            conn, _addr = server.accept()
        except OSError:
            return
        with conn:
            entered.set()
            release.wait(3)

    thread = threading.Thread(target=hold_handshake, daemon=True)
    thread.start()
    stall_config = StallWatchdogConfig(probe_interval_s=0.01, probe_timeout_s=0.01)
    watchdog: guard.StallWatchdog | None = None
    try:
        with (
            pytest.raises(RuntimeError, match=r"database probe .* still in flight"),
            guard.stall_guard(
                f"postgresql://127.0.0.1:{port}/test", port, config=stall_config
            ) as active_watchdog,
        ):
            watchdog = active_watchdog
            assert entered.wait(3)
    finally:
        release.set()
        server.close()
        thread.join(timeout=2)
        if watchdog is not None:
            watchdog.join(timeout=2)
    assert watchdog is not None and not watchdog.is_alive()
