"""`base.telemetry.process_logging.init_*` init functions idempotent protection.

Bug origin: `logger.add()` not idempotent, adds sink each time → same path multiple handlers → fd accumulation. Watchdog daemon runs 6 healthcheck.main() every 60s, each calls ``init_gateway_process()`` → ~1000 fds per hour then OSError errno 24.

Guard makes repeated calls silent skip, consistent with the init functions' docstring "call once at startup" contract.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

import pytest

import base.log.process as slog
from base.host.proc import run_bounded
from base.log.sinks import FILE_SINK_SIZE_LIMIT

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PROCESS_LOGGING = Path(__file__).with_name("_process_logging_child.py")


@pytest.mark.parametrize(
    ("role", "filename", "service_started"),
    [
        ("subprocess", "agent-17.log", False),
        ("cli", "cli-watchdog-update.log", False),
        ("gateway", "restarter.log", True),
        ("default-gateway", "gateway.log", True),
        ("cross-role", "restarter.log", True),
    ],
)
def test_process_logging_opens_each_sink_once(
    tmp_path: Path, role: str, filename: str, service_started: bool
) -> None:
    child = run_bounded(
        [sys.executable, "-I", str(_PROCESS_LOGGING), role],
        env={**os.environ, "MEIVA_HOME": str(tmp_path), "MEIVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    result = json.loads(child.stdout)
    assert result["new_threads"] == [], "process sinks must not allocate enqueue workers"
    assert set(result["files"]) == {filename}
    records = result["files"][filename]
    assert sum(record["message"] == "idempotency-probe" for record in records) == 1
    assert child.stderr.count("idempotency-probe") == (0 if role == "subprocess" else 1)
    _assert_process_events(result["events"], role, filename, service_started)


# meiva:note
# The file follows the home the entry hands in, not the process's ambient home: the child's
# `MEIVA_HOME` is one directory and the entry names another, and only the named one gets a log.
@pytest.mark.parametrize(
    ("role", "filename"),
    [
        ("subprocess", "agent-17.log"),
        ("cli", "cli-watchdog-update.log"),
        ("gateway", "restarter.log"),
        ("default-gateway", "gateway.log"),
    ],
)
def test_the_log_file_follows_the_home_the_entry_hands_in(
    tmp_path: Path, role: str, filename: str
) -> None:
    ambient = tmp_path / "ambient"
    ambient.mkdir()
    explicit = tmp_path / "explicit"
    explicit.mkdir()

    child = run_bounded(
        [sys.executable, "-I", str(_PROCESS_LOGGING), role, str(explicit)],
        env={**os.environ, "MEIVA_HOME": str(ambient), "MEIVA_CONFIG_FETCH": "skip"},
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert child.returncode == 0, child.stderr
    assert set(json.loads(child.stdout)["files"]) == {filename}
    assert (explicit / "logs" / filename).is_file()
    assert list((ambient / "logs").glob("*.log")) == []


def _assert_process_events(
    events: list[dict[str, Any]], role: str, filename: str, service_started: bool
) -> None:
    starts = [event for event in events if event["name"] == "service_started"]
    assert len(starts) == int(service_started)
    if service_started:
        attributes = starts[0]["attributes"]
        assert attributes["name"] == filename.removesuffix(".log")
        assert attributes["sha"] == "test-image"
        assert attributes["host"] == "test-host"
        assert attributes["pid"] > 0
    probes = [event for event in events if event["attributes"].get("msg") == "idempotency-probe"]
    assert len(probes) == (0 if role == "subprocess" else 1)


def test_file_sink_rotates_and_expires(tmp_path: Path) -> None:
    path = tmp_path / "x.log"
    with path.open("wb") as log:
        log.truncate(FILE_SINK_SIZE_LIMIT + 1)
    expired = tmp_path / "x.2000-01-01_00-00-00_000001.log"
    expired.write_text("expired record")
    old = time.time() - 9 * 24 * 60 * 60
    os.utime(expired, (old, old))
    sink_id = slog.add_file_sink(path)
    try:
        slog.logger.warning("rotation-probe")
        assert json.loads(path.read_text())["record"]["message"] == "rotation-probe"
        assert not expired.exists()
        assert any(
            rotated.stat().st_size > FILE_SINK_SIZE_LIMIT for rotated in tmp_path.glob("x.*.log")
        )
    finally:
        slog.logger.remove(sink_id)


# ─── records written while an init builds Settings ─────────────────────────
#
# `init_cli_process` / `init_gateway_process` resolve `logs_dir()` through
# `base.paths`, whose import builds the Settings chain — and a configured
# runner's Settings build is the gateway fetch, which warns when it continues
# on a stale config snapshot (`base/host/env/bootstrap.py`). Importing `base.log`
# dropped loguru's default handler, so that warning reaches stderr only if the
# init opened the stderr sink before the import.

_SETTINGS_BUILD_WARNING = Path(__file__).with_name("_log_init_child.py")


@pytest.mark.parametrize("init", ["init_cli_process", "init_gateway_process"])
def test_a_warning_written_while_the_init_builds_settings_reaches_stderr(
    tmp_path: Path, init: str
) -> None:
    import json
    import os
    import subprocess
    import sys
    import time

    from base.host.env import bootstrap

    home = tmp_path / "home"
    (home / "run").mkdir(parents=True)
    gateway = "http://127.0.0.1:1"
    # A configured pure runner: its `.env` carries the role, the gateway URL and the
    # name the gateway init gives its host.
    (home / ".env").write_text(
        f"MEIVA_MACHINE_NAME=probe-host\nMEIVA_MACHINE_SERVE_AGENT_RUNNER=true\nMEIVA_GATEWAY_URL={gateway}\n"
    )
    (home / ".env").chmod(0o600)
    (home / "run" / "bootstrap-snapshot.json").write_text(
        json.dumps(
            {
                "v": bootstrap.SNAPSHOT_VERSION,
                "base_url": gateway,
                "written_at": time.time() - 10_000,
                "values": {
                    "MEIVA_DB_URL": "postgresql://meiva@127.0.0.1:1/meiva",
                    "MEIVA_REDIS_URL": "redis://127.0.0.1:1/0",
                },
            }
        )
    )
    env = {key: value for key, value in os.environ.items() if not key.startswith("MEIVA_")}
    env.update(MEIVA_HOME=str(home))
    child = subprocess.run(  # noqa: S603 — this interpreter, fixed child script, a private home
        [sys.executable, "-I", "-B", str(_SETTINGS_BUILD_WARNING), str(_REPO_ROOT), init],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert child.returncode == 0, child.stderr
    assert "continuing on the last-known cluster config snapshot" in child.stderr


# ─── owner-only log files (audit round-2 up-security-trust P1-1) ───────────


def test_add_file_sink_tightens_permissions(tmp_path: Path) -> None:
    """Log files carry full agent exec output, so add_file_sink makes the
    logs dir 0700 and the file 0600 — content must not be world-readable even
    under a permissive umask."""
    log_path = tmp_path / "logs" / "agent-1.log"
    sink_id = slog.add_file_sink(log_path)
    try:
        assert (log_path.parent.stat().st_mode & 0o777) == 0o700
        assert (log_path.stat().st_mode & 0o777) == 0o600
    finally:
        slog.logger.remove(sink_id)
        log_path.unlink()


# ─── every loguru sink forces diagnose=False (P1 security fix) ─────────────
#
# loguru's own default is `diagnose=True`: on a formatted exception it renders
# every local variable's value from every frame of the traceback into the
# sink's output. `base.log.sinks.add_sink` is the seam every sink goes
# through, and it forces `diagnose=False`; `scripts/lint/diagnostics/logger_add_diagnose.py`
# keeps every other `logger.add(...)` call honest.


def test_a_password_bearing_dsn_failure_via_logger_exception_never_leaks(
    postgres_access: None,
) -> None:
    """The second leak path the PR #3479 review confirmed independently of
    `RoleSecret`: `psycopg.connect(dsn)` against a password-bearing DSN, on
    connect failure, logged via `logger.exception(...)`. `connect()` itself
    is a real function in a real file, so diagnose's `linecache`-based
    per-frame annotation applies to it directly, no subprocess needed —
    this test's own line (`psycopg.connect(dsn, ...)`) is also a real
    source line referencing `dsn`, which is exactly the shape that would
    leak if this sink's `diagnose` were not forced off."""
    import psycopg

    captured: list[str] = []

    def _capture(message: object) -> None:
        captured.append(str(message))

    sink_id = slog.add_sink(_capture, level="DEBUG")
    sentinel = "SENTINEL-DSN-PASSWORD-9876543210"
    dsn = f"postgresql://u:{sentinel}@127.0.0.1:1/x"
    try:
        try:
            psycopg.connect(dsn, connect_timeout=2)
        except Exception:
            slog.logger.exception("db connect failed")
        else:
            pytest.fail("connect to an unassigned port must fail")
    finally:
        slog.logger.remove(sink_id)

    blob = "\n".join(captured)
    assert "db connect failed" in blob
    assert sentinel not in blob
