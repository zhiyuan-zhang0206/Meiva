"""`base.telemetry.process_logging.init_*` init functions idempotent protection.

Bug origin: `logger.add()` not idempotent, adds sink each time → same path multiple handlers → fd accumulation. Watchdog daemon runs 6 healthcheck.main() every 60s, each calls ``init_gateway_process()`` → ~1000 fds per hour then OSError errno 24.

Guard makes repeated calls silent skip, consistent with the init functions' docstring "call once at startup" contract.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

import base.log.process as slog
import base.telemetry.process_logging as plog
from base.native_process.loaded_commit import LoadedCommit
from base.paths import logs_dir
from base.telemetry.delivery.pipeline import EventPipeline

_REPO_ROOT = Path(__file__).resolve().parents[4]


def _unused_producer() -> EventPipeline:
    raise AssertionError("the mocked sink must not construct a pipeline")


def gateway_inputs(**overrides: Any) -> dict[str, Any]:
    """Explicit wiring inputs; sink mocks prevent constructing the test producer."""
    base: dict[str, Any] = {
        "producer": _unused_producer,
        "machine_reader": lambda: "test-host",
        "image": LoadedCommit(_REPO_ROOT, "test-image"),
        "stderr_sink_fn": lambda *_a, **_kw: 1,
        "file_sink_fn": lambda _p: 2,
        "event_sink_fn": lambda **_kw: 3,
        "info_fn": lambda *_a, **_kw: None,
    }
    base.update(overrides)
    return base


@pytest.fixture(autouse=True)
def reset_init_flag() -> Iterator[None]:
    """Reset module-level guard between tests to avoid cross-contamination.

    In production process init happens only once; test fixture-level reset is for test isolation.
    """
    slog.reset_init_done()
    yield
    slog.reset_init_done()


def test_init_gateway_process_idempotent() -> None:
    """Repeated calls to init_gateway_process trigger only one logger.add chain."""
    stderr_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    file_calls: list[Path] = []
    event_calls: list[dict[str, Any]] = []
    inputs = gateway_inputs(
        stderr_sink_fn=lambda *a, **kw: stderr_calls.append((a, kw)) or 1,
        file_sink_fn=lambda p: file_calls.append(p) or 2,
        event_sink_fn=lambda **kw: event_calls.append(kw) or 3,
    )
    plog.init_gateway_process(**inputs)
    plog.init_gateway_process(**inputs)
    plog.init_gateway_process(**inputs)

    # three adds should only happen once: stderr / file / postgres group
    assert len(stderr_calls) == 1, "stderr sink should be added only once"
    assert len(file_calls) == 1, "file sink should be added only once"
    assert len(event_calls) == 1, "event sink should be added only once"


def test_init_subprocess_logger_idempotent() -> None:
    """Repeated calls to init_subprocess_logger trigger only one sink chain."""
    configure_calls: list[dict[str, Any]] = []
    file_calls: list[Path] = []
    slog.init_subprocess_logger(
        agent_id=1,
        configure_fn=lambda **kw: configure_calls.append(kw),
        file_sink_fn=lambda p: file_calls.append(p) or 1,
    )
    slog.init_subprocess_logger(
        agent_id=1,
        configure_fn=lambda **kw: configure_calls.append(kw),
        file_sink_fn=lambda p: file_calls.append(p) or 1,
    )

    assert len(configure_calls) == 1
    assert len(file_calls) == 1


def test_first_init_wins_subsequent_silent_skip() -> None:
    """Call init_gateway first, then init_subprocess_logger → second silent skip doesn't mix sink types.

    Single-init contract: at process startup, one role (gateway / subprocess / cli) is determined,
    then init_done locked, init of other roles also goes silent skip. Otherwise, beyond watchdog daemon
    (gateway init) calling healthcheck.main() (also gateway init), if healthcheck mistakenly calls different role like init_subprocess_logger, sinks would accumulate again.
    """
    stderr_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    file_calls: list[Path] = []
    plog.init_gateway_process(
        **gateway_inputs(
            stderr_sink_fn=lambda *a, **kw: stderr_calls.append((a, kw)) or 1,
            file_sink_fn=lambda p: file_calls.append(p) or 2,
        )
    )
    # second time switching roles — still skip
    slog.init_subprocess_logger(
        agent_id=1,
        configure_fn=lambda **_kw: None,
        file_sink_fn=lambda p: file_calls.append(p) or 2,
    )

    assert len(stderr_calls) == 1, "second cross-role init still must silent skip"
    assert len(file_calls) == 1, "the subprocess role must not add its agent log file"


def test_init_gateway_process_per_daemon_log_file() -> None:
    """init_gateway_process(name=X) writes X.log, not shared gateway.log — each daemon's log (including uncaught exception traceback) lands in per-daemon file, postmortem can be found (shared gateway.log mixing all daemons by pid makes traceability hard)."""
    file_calls: list[Path] = []
    plog.init_gateway_process(
        name="restarter",
        **gateway_inputs(file_sink_fn=lambda p: file_calls.append(p) or 2),
    )

    assert file_calls == [logs_dir() / "restarter.log"]


def test_init_gateway_process_default_name_is_gateway() -> None:
    """Default name remains gateway.log (gateway process backward compat)."""
    file_calls: list[Path] = []
    plog.init_gateway_process(**gateway_inputs(file_sink_fn=lambda p: file_calls.append(p) or 2))

    assert file_calls == [logs_dir() / "gateway.log"]


def test_init_cli_process_idempotent() -> None:
    """Repeated calls to init_cli_process trigger only one sink chain."""
    stderr_calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    file_calls: list[Path] = []
    event_calls: list[dict[str, Any]] = []
    kwargs: dict[str, Any] = {
        "name": "cli-spawn-update-1",
        "producer": _unused_producer,
        "machine_reader": lambda: "test-host",
        "stderr_sink_fn": lambda *a, **kw: stderr_calls.append((a, kw)) or 1,
        "file_sink_fn": lambda p: file_calls.append(p) or 2,
        "event_sink_fn": lambda **kw: event_calls.append(kw) or 3,
    }
    plog.init_cli_process(**kwargs)
    plog.init_cli_process(**kwargs)
    plog.init_cli_process(**kwargs)

    assert len(stderr_calls) == 1
    assert len(file_calls) == 1
    assert len(event_calls) == 1


def test_init_cli_process_per_invocation_log_file() -> None:
    """init_cli_process(name=X) writes X.log — each detached CLI invocation
    isolated to its own file (matches init_gateway_process per-daemon
    naming convention)."""
    file_calls: list[Path] = []
    plog.init_cli_process(
        name="cli-watchdog-update",
        producer=_unused_producer,
        machine_reader=lambda: "test-host",
        stderr_sink_fn=lambda *_a, **_kw: 1,
        file_sink_fn=lambda p: file_calls.append(p) or 2,
        event_sink_fn=lambda **_kw: 3,
    )

    assert file_calls == [logs_dir() / "cli-watchdog-update.log"]


def test_file_sink_rotates_and_expires(tmp_path: Path) -> None:
    """file sink must carry rotation + retention.

    Without them files only monotonically grow, and nothing reclaims: actual measurement gateway.log ~900 MB, agent-N.log ~190 MB. This is not optional tuning for the sink, it's a prerequisite for its usability.
    """
    captured: list[dict[str, Any]] = []
    slog.add_file_sink(
        tmp_path / "x.log",
        add_sink_fn=lambda _sink, **kw: captured.append(kw) or 1,
    )

    kwargs = captured[0]
    assert kwargs["rotation"], "file sink must rotate"
    assert kwargs["retention"], "old rotated files must expire and be reclaimed"


def test_file_sink_never_enqueues(tmp_path: Path) -> None:
    """enqueue must remain False — this locks a previously exploded pitfall, don't "optimize" back.

    `agent-{N}.log` is shared by the agent's exec subprocesses; loguru's official answer for this shared sink is enqueue=True. But that queue allocates POSIX semaphore, and force-killed processes (agent routinely SIGKILL) permanently leak it; when leak accumulates hits kern.posix.sem.max, every new agent startup dies with errno 28.
    Prefer accepting rare interleaving on rotation than switching to enqueue.
    """
    captured: list[dict[str, Any]] = []
    slog.add_file_sink(
        tmp_path / "x.log",
        add_sink_fn=lambda _sink, **kw: captured.append(kw) or 1,
    )

    assert captured[0].get("enqueue", False) is False


def test_init_gateway_process_emits_service_started() -> None:
    """Every gateway-style process boot logs one structured `service_started`
    event — the ops panel restart-count source. One process = one row (the
    init guard makes repeat calls silent skips), and the payload carries the
    daemon identity + pid + loaded commit + host so restarts are attributable.
    """
    infos: list[dict[str, Any]] = []
    inputs = gateway_inputs(info_fn=lambda _msg, **kw: infos.append(kw))
    plog.init_gateway_process(name="restarter", **inputs)
    plog.init_gateway_process(name="restarter", **inputs)  # idempotent — still one row

    assert len(infos) == 1
    kw = infos[0]
    assert kw["event"] == "service_started"
    assert kw["name"] == "restarter"
    assert isinstance(kw["pid"], int) and kw["pid"] > 0
    assert kw["host"]  # machine_name() resolves on the test host


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
