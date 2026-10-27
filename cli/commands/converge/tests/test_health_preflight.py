# meiva:note
# Health preflight — the start-time data-plane + checkout self-check (#607).
#
# `meiva start` / `meiva converge` probe the data plane (pg/redis, the URLs the runtime
# dials) and the checkout (dirty marker). Warning-only, logged to
# `$MEIVA_HOME/logs/health_preflight.log`, never failing a start.


import random
import socket
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base import cluster
from base.cluster.machine import MachineRoles
from base.host.env import port_lease
from base.telemetry import EventPipeline
from cli import start_identity
from cli.commands.converge import health_preflight as _hp
from cli.commands.converge.spec import ConvergeCtx
from cli.database.runtime import CommandRuntime, UserSession
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.cli_tests import operator_pipeline as operator_pipeline
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.startup.tests.birth_facts import birth_facts


# meiva:note
# One port held by the host-wide lease for this session; sibling workers cannot reuse it.
def _free_port() -> int:
    return port_lease.lease_ports(1)[0]


# meiva:note
# URLs of a plane nothing serves: both probes are refused, as on a host whose plane is down.
def _dead_urls() -> tuple[str, str]:
    return (
        f"postgresql://meiva:sek@127.0.0.1:{_free_port()}/meiva",
        f"redis://:sek@127.0.0.1:{_free_port()}/0",
    )


def _preflight_ctx(
    tmp_path: Path,
    urls: tuple[str, str],
    roles: MachineRoles | None = frozenset({"gateway"}),
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> ConvergeCtx:
    config = command_runtime.config
    config.set_field("db_url", urls[0])
    config.set_field("redis_url", urls[1])
    return ConvergeCtx(
        secure_rng=random.SystemRandom(),
        repo=tmp_path / "repo",
        meiva_home=tmp_path / "home",
        roles=roles,
        config=config,
        database_factory=operator_database,
        producer=producer,
        machine=command_runtime.machine,
        helper=command_runtime.helper,
        host=command_runtime.host,
        session=UserSession(user_home=tmp_path / "home", login_shell="/bin/zsh", interactive=False),
        services=frozenset(),
        timers=command_runtime.timers,
        clock=command_runtime.clock,
    )


# meiva:note
# The probed endpoints a warning list names, in order: each line is `<kind> <url>: <reason>`.
def _endpoints(warnings: list[str], *urls: str) -> bool:
    prefixes = [
        ("postgres" if u.startswith("postgresql") else "redis") + f" {_hp.redact(u)}: "
        for u in urls
    ]
    return len(warnings) == len(prefixes) and all(
        w.startswith(prefix) for w, prefix in zip(warnings, prefixes, strict=True)
    )


# meiva:note
# A gateway home that has been through `meiva init`: its start intent carries the registry record
# whose ports the preflight reads. Nothing listens on them.
def _born_gateway_home(home: Path) -> cluster.ClusterRecord:
    home.mkdir(parents=True, exist_ok=True)
    start_identity.prepare_identity(
        start_identity.IdentityInput(
            home,
            home,
            frozenset({"gateway"}),
            {"MEIVA_MACHINE_NAME": "gw", "MEIVA_GATEWAY_URL": "http://127.0.0.1:1"},
            occupied_ports=frozenset(),
            birth=birth_facts(),
        )
    )
    record = cluster.get_record(home)
    assert record is not None
    return record


# meiva:note
# A data-plane finding -> the start CONTINUES (rc-free step) but prints the warning and
# appends it to $MEIVA_HOME/logs/health_preflight.log.
def test_health_preflight_warns_and_logs(
    postgres_access: None,
    redis_access: None,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    pg_url, redis_url = _dead_urls()
    ctx = _preflight_ctx(
        tmp_path,
        (pg_url, redis_url),
        roles=frozenset({"agent-runner"}),
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    _hp.ensure_health_preflight(ctx)  # must not raise — a preflight never fails a start

    err = capsys.readouterr().err
    assert "HEALTH PREFLIGHT" in err
    assert f"postgres {_hp.redact(pg_url)}:" in err and f"redis {_hp.redact(redis_url)}:" in err
    log = tmp_path / "home" / "logs" / "health_preflight.log"
    lines = log.read_text().splitlines()
    assert len(lines) == 2
    assert f"postgres {_hp.redact(pg_url)}:" in lines[0]
    assert f"redis {_hp.redact(redis_url)}:" in lines[1]


# meiva:note
# A reachable plane and no checkout finding -> no output, no log file.
def test_health_preflight_silent_when_clean(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    db_url: str,
    redis_url: str,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    ctx = _preflight_ctx(
        tmp_path,
        (db_url, redis_url),
        roles=frozenset({"agent-runner"}),
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    _hp.ensure_health_preflight(ctx)

    assert capsys.readouterr().err == ""
    assert not (tmp_path / "home" / "logs" / "health_preflight.log").exists()


# meiva:note
# A scan exception prints a notice and returns — the step must not turn a warning pass into
# a failed start. A gateway home with no recorded ports makes the scan raise.
def test_health_preflight_never_fails_start_on_scan_error(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    ctx = _preflight_ctx(
        tmp_path,
        _dead_urls(),
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    _hp.ensure_health_preflight(ctx)

    err = capsys.readouterr().err
    assert "health preflight skipped: gateway home" in err
    assert "has no recorded ports" in err


# meiva:note
# Gateway with neither pg nor redis bound -> nothing probed, no warnings: the start sequence
# brings the instance up right after converge, so probing now would warn on every boot. The URLs
# are dead, so a probe would have warned.
def test_data_plane_gateway_cold_start_skipped(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    _born_gateway_home(tmp_path / "home")
    ctx = _preflight_ctx(
        tmp_path,
        _dead_urls(),
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    assert _hp.data_plane_warnings(ctx) == []


# meiva:note
# Gateway with one of the two bound -> probes both: the bound port is not a Postgres, and
# Redis is down.
def test_data_plane_gateway_partial_up_probes(
    postgres_access: None,
    redis_access: None,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    record = _born_gateway_home(tmp_path / "home")
    pg_url, redis_url = _dead_urls()
    with socket.socket() as occupant:
        occupant.bind(("127.0.0.1", record.ports["postgres"]))
        occupant.listen(1)
        ctx = _preflight_ctx(
            tmp_path,
            (pg_url, redis_url),
            operator_database=operator_database,
            producer=operator_pipeline,
            command_runtime=command_runtime,
        )

        warnings = _hp.data_plane_warnings(ctx)

    assert _endpoints(warnings, pg_url, redis_url), warnings


# meiva:note
# A runner's plane is remote — an unreachable one is the real signal, so even a fully cold
# host probes (the runner has no local bring-up to fix it).
def test_data_plane_runner_probes_unconditionally(
    postgres_access: None,
    redis_access: None,
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    pg_url, redis_url = _dead_urls()
    ctx = _preflight_ctx(
        tmp_path,
        (pg_url, redis_url),
        roles=frozenset({"agent-runner"}),
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    warnings = _hp.data_plane_warnings(ctx)

    assert _endpoints(warnings, pg_url, redis_url), warnings


# meiva:note
# roles=None (unit not configured yet) -> nothing probed (the URLs are dead, so a probe would
# have warned) and the configuration is left unread.
def test_data_plane_unconfigured_skipped(
    tmp_path: Path,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
) -> None:
    ctx = _preflight_ctx(
        tmp_path,
        _dead_urls(),
        roles=None,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    was_prepared = ctx.config.prepared
    assert _hp.data_plane_warnings(ctx) == []
    assert ctx.config.prepared == was_prepared


def test_preflight_keeps_reading_its_owners_live_urls(
    tmp_path: Path,
    db_url: str,
    redis_url: str,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> None:
    ctx = _preflight_ctx(
        tmp_path,
        (db_url, redis_url),
        roles=frozenset({"agent-runner"}),
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    assert _hp.data_plane_warnings(ctx) == []
    changed_pg, changed_redis = _dead_urls()
    ctx.config.set_field("db_url", changed_pg)
    ctx.config.set_field("redis_url", changed_redis)

    warnings = _hp.data_plane_warnings(ctx)

    assert _endpoints(warnings, changed_pg, changed_redis), warnings


# meiva:note
# A dead endpoint yields an error string, not an exception. (The exact
# wording differs by platform — "Connection refused" on Linux, "Connection
# reset" on macOS — so only the failure is asserted.)
def test_probe_postgres_reports_refused(postgres_access: None):
    err = _hp.probe_postgres(f"postgresql://u:p@127.0.0.1:{_free_port()}/db")
    assert err is not None and "connection" in err.lower()


# meiva:note
# A dead endpoint yields an error string, not an exception. (Wording varies
# by platform — see probe_postgres's twin.)
def test_probe_redis_reports_refused(redis_access: None):
    err = _hp.probe_redis(f"redis://127.0.0.1:{_free_port()}/0")
    assert err is not None and "connection" in err.lower()


def test_redact_strips_userinfo():
    assert _hp.redact("postgresql://user:sekret@127.0.0.1:5433/db") == (
        "postgresql://127.0.0.1:5433/db"
    )


# meiva:note
# A real git repo with two commits → (ctx, head_sha, first_sha).
def _git_repo(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    operator_database: Callable[[], Any],
    producer: Callable[[], EventPipeline],
    command_runtime: CommandRuntime,
) -> tuple[ConvergeCtx, str, str]:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }

    def _commit(msg: str) -> str:
        (repo / "f.txt").write_text(msg + "\n")
        subprocess.run(
            ["git", "add", "f.txt"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            env=env,
        )
        subprocess.run(
            ["git", "commit", "-q", "-m", msg],
            cwd=str(repo),
            check=True,
            capture_output=True,
            env=env,
        )
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo),
            check=True,
            capture_output=True,
            text=True,
            env=env,
        ).stdout.strip()

    first = _commit("one")
    head = _commit("two")
    ctx = ConvergeCtx(
        secure_rng=random.SystemRandom(),
        repo=repo,
        meiva_home=tmp_path / "home",
        roles=frozenset({"gateway"}),
        config=command_runtime.config,
        database_factory=operator_database,
        producer=producer,
        machine=command_runtime.machine,
        helper=command_runtime.helper,
        host=command_runtime.host,
        session=UserSession(user_home=tmp_path / "home", login_shell="/bin/zsh", interactive=False),
        services=frozenset(),
        timers=command_runtime.timers,
        clock=command_runtime.clock,
    )
    return ctx, head, first


# meiva:note
# Uncommitted changes in the source tree → a warning naming the count.
def test_checkout_dirty_warns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    ctx, _head, _first = _git_repo(
        tmp_path,
        monkeypatch,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )
    (tmp_path / "repo" / "stray.txt").write_text("uncommitted\n")

    warnings = _hp.checkout_warnings(ctx)

    assert any("dirty (1 changed file" in w for w in warnings)


# meiva:note
# A clean checkout at any commit warns nothing: there is no cluster target
# commit to compare its HEAD against.
def test_checkout_clean_is_silent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    ctx, _head, _first = _git_repo(
        tmp_path,
        monkeypatch,
        operator_database=operator_database,
        producer=operator_pipeline,
        command_runtime=command_runtime,
    )

    assert _hp.checkout_warnings(ctx) == []


# meiva:note
# A dev worktree path → checkout state is skipped entirely (dev context).
def test_checkout_worktree_skipped(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    operator_database: Callable[[], Any],
    operator_pipeline: Callable[[], EventPipeline],
    *,
    command_runtime: CommandRuntime,
):
    wt = tmp_path / ".claude" / "worktrees" / "some-task"
    wt.mkdir(parents=True)
    ctx = ConvergeCtx(
        secure_rng=random.SystemRandom(),
        repo=wt,
        meiva_home=tmp_path / "home",
        roles=frozenset({"gateway"}),
        config=command_runtime.config,
        database_factory=operator_database,
        producer=operator_pipeline,
        machine=command_runtime.machine,
        helper=command_runtime.helper,
        host=command_runtime.host,
        session=UserSession(user_home=tmp_path / "home", login_shell="/bin/zsh", interactive=False),
        services=frozenset(),
        timers=command_runtime.timers,
        clock=command_runtime.clock,
    )
    (wt / "stray.txt").write_text("dirty\n")

    assert _hp.checkout_warnings(ctx) == []
