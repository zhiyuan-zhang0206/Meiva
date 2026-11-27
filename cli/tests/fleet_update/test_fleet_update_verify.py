from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import cast

import pytest

from base.db import connect_url
from base.host.env.boot_policy import MIRROR_HOST_PARAM, MirrorRejectedError, issue_config_mirror
from base.host.env.bootstrap import read_config_mirror
from base.host.env.config_lite_table import BUILTIN_CLUSTER_CONFIG_FIELDS
from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV
from base.host.net.tests.local_http import serve_json
from base.paths import repo_root
from base.paths.tests.fake_scratch import FakeScratch
from cli.fleet_update.choices import parse
from cli.fleet_update.fleet_verify import roster
from cli.fleet_update.plan.host_failures import PARTIAL_FAILURE_EXIT
from cli.fleet_update.probes.config import authorize_mirror, fetch_mirror
from cli.fleet_update.probes.receipt import DomainReceipt, receipt_from_output
from cli.fleet_update.session import FailedError, Pacing, Session
from cli.tests.fleet_update.fleet_update_cluster import Cluster, git, run_host_shell

# meiva:note
# One roster reply that never settles: the machine is listed and online, but its running commit
# differs from its head, so the wait has to look again.
_UNSETTLED_ROSTER = json.dumps(
    [
        {
            "name": "mac-mini",
            "online": True,
            "identity_mismatch": False,
            "head_sha": "a" * 40,
            "running_sha": "b" * 40,
            "serve_agent_runner": True,
        }
    ]
)


# meiva:note
# The verification answers for every host, whatever the others did: one machine's failed check
# must not delete what the window learned about the rest, so the run still verifies them and still
# reports a line per host.
def test_one_failed_refresh_still_verifies_and_reports_every_host(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("refresh", "mac")] = 1
    assert run("up") == PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    matrix = [line.partition("matrix: ")[2] for line in out.splitlines() if "matrix: " in line]
    assert [row.split()[0] for row in matrix] == ["gw", "mac", "lin"]
    assert matrix[0].endswith("serving") and matrix[2].endswith("serving")
    assert matrix[1].startswith("mac failed at 'packages refresh'")
    assert "plugins verify" in out and "schedules verify" in out


def test_a_host_that_never_started_is_reported_skipped_not_missing(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("start", "lin")] = 1
    assert run("up") == PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    matrix = [line.partition("matrix: ")[2] for line in out.splitlines() if "matrix: " in line]
    assert len(matrix) == 3
    assert matrix[0] == "gw serving" and matrix[1] == "mac serving"
    assert matrix[2].startswith("lin failed at 'start'")


def test_a_dry_run_says_the_helper_gate_was_not_evaluated(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    _, run = env
    assert run("run", "--dry-run") == 0
    out = capsys.readouterr().out
    assert "helper gate: NOT evaluated" in out
    assert out.count("matrix: ") == 3 and "skipped: dry run" in out


# meiva:note
# A counter standing in for the process clock: every sleep is recorded and moves the counter, so a
# window of whole seconds costs no wall time.
class _Timers:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


# meiva:note
# The one gateway answer the roster wait reads, with the pacing and the clock the session carries.
# It counts the reads and refuses to go on after `most_reads`: the window is two seconds of a fake
# clock, so a wait that stopped advancing its clock would otherwise poll for ever, and this suite
# has no per-test timeout to stop it.
class _Gateway:
    def __init__(self, reply: str, pacing: Pacing, timers: _Timers, *, most_reads: int) -> None:
        self.reply = reply
        self.pacing = pacing
        self.timers = timers
        self.most_reads = most_reads
        self.reads = 0
        self.said: list[str] = []

    def run(
        self, alias: str, command: str, *, effect: bool = True, stdin: str | None = None
    ) -> str:
        self.reads += 1
        if self.reads > self.most_reads:
            raise AssertionError("the roster wait never bounded its polls")
        return self.reply

    def say(self, line: str) -> None:
        self.said.append(line)


# meiva:note
# The roster wait is spent on the clock the session carries. A wait that read `time` for itself
# would record no sleep here and burn the same two seconds of wall time instead.
def test_the_roster_wait_is_spent_on_the_sessions_clock(tmp_path: Path) -> None:
    timers = _Timers()
    session = cast(
        Session,
        _Gateway(_UNSETTLED_ROSTER, Pacing(roster_poll_s=0.5), timers, most_reads=5),
    )
    args = parse(
        [
            "up",
            "--gateway",
            "gw",
            "--no-runners",
            "--log-dir",
            "logs",
            "--execute",
            "--all-services",
            "--start-timeout",
            "60",
            "--roster-timeout",
            "2",
        ],
        {},
        python=sys.executable,
        scratch=FakeScratch(tmp_path / "scratch"),
    )
    started = time.monotonic()

    with pytest.raises(FailedError):
        roster(session, args, {"mac": "mac-mini"}, "stdin", expect_offline=frozenset())

    assert timers.sleeps == [0.5, 0.5, 0.5, 0.5]
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("fault", ["valid", "generation", "host", "expired", "short", "legacy"])
def test_prestage_requests_and_atomically_publishes_only_authorized_mirrors(
    tmp_path: Path, fault: str
) -> None:
    host = "runner.example"
    bearer = "fixture-bearer"
    now = 1_000_000
    values = {
        "MEIVA_DB_URL": "postgresql://gateway/db",
        PLUGIN_CLUSTER_CONFIG_ENV: json.dumps(
            {
                plugin: dict.fromkeys(fields)
                for plugin, fields in BUILTIN_CLUSTER_CONFIG_FIELDS.items()
            }
        ),
    }
    envelope = issue_config_mirror(
        values, bearer=bearer, machine_host="other.example" if fault == "host" else host, now=now
    )
    if fault == "generation":
        envelope["generation"] = "0" * 64
    if fault in ("expired", "short"):
        now = envelope["expires_at"] - (1 if fault == "short" else 0)
    with serve_json(values if fault == "legacy" else envelope) as server:
        env = {
            "MEIVA_GATEWAY_URL": server.url,
            "MEIVA_MACHINE_HOST": host,
            "MEIVA_API_TOKEN": bearer,
        }

        def stage() -> None:
            _ = authorize_mirror(
                env,
                home=tmp_path,
                stage=True,
                now=lambda: now,
                minimum_lifetime=60,
                fetch=fetch_mirror,
            )

        if fault == "valid":
            stage()
            installed = read_config_mirror(home=tmp_path, bearer=bearer, machine_host=host, now=now)
            assert installed == envelope
        else:
            with pytest.raises(MirrorRejectedError):
                stage()
            assert not (tmp_path / "bootstrap-mirror-enrolled").exists()
            assert not (tmp_path / "run" / "bootstrap-mirror.json").exists()
    assert len(server.requests) == 1
    assert server.requests[0].path == f"/api/bootstrap?{MIRROR_HOST_PARAM}={host}"
    assert server.requests[0].headers["authorization"] == f"Bearer {bearer}"


def _domain_line(target: str, *, config: str, applied: str, components: dict[str, str]) -> str:
    checks = {"checkpoint": "ready", **components}
    return DomainReceipt(target, "runner", config, "e" * 64, applied, checks).line()


def test_down_waits_for_every_target_receipt_before_first_stop(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env

    assert run("down") == 0

    scripts = [script for _, script in cluster.commands]
    staged = [i for i, script in enumerate(scripts) if "probes prepare" in script]
    destructive = [i for i, script in enumerate(scripts) if 'meiva" stop' in script]
    assert len(staged) == len(destructive) == 3
    assert max(staged) < min(destructive)
    assert all(cluster.new in scripts[i] for i in staged)


@pytest.mark.parametrize(
    ("fault", "detail"),
    [
        ("expired", "expired mirror"),
        ("legacy", "domain receipt version"),
        ("checkpoint", "checkpoint schema is not current"),
        ("config", "runner config generation"),
        ("schema", "schema version"),
        ("harness", "harness/codex"),
    ],
)
def test_invalid_compatibility_evidence_refuses_the_entire_stop_wave(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    fault: str,
    detail: str,
) -> None:
    cluster, run = env
    receipt = _domain_line(
        cluster.new,
        config=("b" if fault == "config" else "c") * 64,
        applied=("a" if fault == "schema" else "e") * 64,
        components={
            "config": "ready",
            "schema": "migration-pending" if fault == "schema" else "ready",
            "checkpoint": "checkpoint schema is not current" if fault == "checkpoint" else "ready",
            "harness/codex": "version is incompatible" if fault == "harness" else "ready",
        },
    )
    cluster.probe_replies[("prepare", "lin")] = {
        "expired": (1, "domain config: expired mirror"),
        "legacy": (0, receipt.replace('"version": 2', '"version": 1')),
    }.get(fault, (0, receipt))

    assert run("run") == 2

    assert not {"stop", "silence-open", "switch"} & {kind for kind, _ in cluster.effects}
    assert detail in capsys.readouterr().out
    assert all(host["head"] == cluster.old for host in cluster.hosts.values())


def test_live_process_proof_does_not_hide_schedule_readiness_failure(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.probe_replies[("ready", "lin")] = (
        0,
        _domain_line(
            cluster.new,
            config="c" * 64,
            applied="e" * 64,
            components={"config": "ready", "schema": "ready", "schedule-manager": "down"},
        ),
    )
    cluster.probe_replies[("supervision", "mac")] = (1, "meiva-root: missing unit pty-sessions")

    assert run("run") == PARTIAL_FAILURE_EXIT

    output = capsys.readouterr().out
    assert f"lin| FLEET_SUPERVISION={cluster.new}" in output
    assert "lin failed at 'domain readiness': schedule-manager: down" in output
    assert "mac failed at 'process supervision'" in output
    assert f"config {'c' * 64}" in output
    assert f"schema {'e' * 64}" in output


def test_up_only_reports_an_explicit_legacy_degradation(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    for phase in ("supervision", "ready"):
        cluster.probe_replies[(phase, "mac")] = (0, "FLEET_PROBE_UNSUPPORTED")

    assert run("up") == 0

    output = capsys.readouterr().out
    assert "mac: process supervision unavailable on this legacy checkout" in output
    assert "mac: domain readiness unavailable on this legacy checkout" in output


def _probe_home(tmp_path: Path, db_url: str, redis_url: str) -> Path:
    home = tmp_path / ".meiva"
    home.mkdir()
    _ = (home / ".env").write_text(
        "MEIVA_MACHINE_NAME=fixture-gateway\n"
        "MEIVA_MACHINE_HOST=127.0.0.1\n"
        "MEIVA_MACHINE_SERVE_GATEWAY=true\n"
        f"MEIVA_DB_URL={db_url}\n"
        f"MEIVA_REDIS_URL={redis_url}\n"
    )
    return home


def _target_probe(
    home: Path, phase: str, target: str, *selection: str
) -> subprocess.CompletedProcess[str]:
    repo = repo_root()
    return run_host_shell(
        [sys.executable, "-m", "cli.fleet_update.probes", phase, target, *selection],
        cwd=repo,
        env={
            "HOME": str(home.parent),
            "PATH": os.environ["PATH"],
            "MEIVA_HOME": str(home),
            "MEIVA_CONFIG_FETCH": "skip",
        },
        timeout=30,
    )


def test_target_probe_reads_real_applied_schema_and_persists_only_receipt_metadata(
    tmp_path: Path, db_url: str, redis_url: str
) -> None:
    home = _probe_home(tmp_path, db_url, redis_url)
    target = git(repo_root(), "rev-parse", "HEAD")
    result = _target_probe(home, "prepare", target, "--all-services")

    assert result.returncode == 0, (result.stdout, result.stderr)
    receipt = receipt_from_output(result.stdout, target=target)
    assert receipt.applied == receipt.schema
    assert receipt.components["schema"] == "ready"
    assert receipt.components["schedule-manager"] == "selected"
    path = home / "updates" / "fleet-domain-receipt.json"
    assert path.stat().st_mode & 0o777 == 0o600
    assert receipt.components["checkpoint"] == "ready"
    assert path.read_text() == receipt.encode()
    assert db_url not in result.stdout and db_url not in path.read_text()


@pytest.mark.parametrize("phase", ["prepare", "ready"])
def test_target_probe_rejects_a_gap_in_real_checkpoint_migrations(
    tmp_path: Path, db_url: str, redis_url: str, phase: str
) -> None:
    home = _probe_home(tmp_path, db_url, redis_url)
    target = git(repo_root(), "rev-parse", "HEAD")
    with connect_url(db_url) as conn:
        deleted = conn.execute("DELETE FROM checkpoint_migrations WHERE v = 0").rowcount
    assert deleted == 1
    try:
        result = _target_probe(home, phase, target, "--only-service", "schedule-manager")
    finally:
        with connect_url(db_url) as conn:
            _ = conn.execute("INSERT INTO checkpoint_migrations (v) VALUES (0)")

    assert result.returncode == 0, (result.stdout, result.stderr)
    receipt = receipt_from_output(result.stdout, target=target)
    assert receipt.components["schema"] == "ready"
    assert "missing=[0]" in receipt.components["checkpoint"]


@pytest.mark.parametrize("version", ["target", "stale", "missing"])
def test_target_readiness_requires_the_full_sha_from_the_live_service(
    tmp_path: Path, db_url: str, redis_url: str, version: str
) -> None:
    home = _probe_home(tmp_path, db_url, redis_url)
    target = git(repo_root(), "rev-parse", "HEAD")
    stale = target[:7] + ("a" if target[7] != "a" else "b") + target[8:]
    sha = {"target": target, "stale": stale, "missing": None}[version]
    pidfile = home / "run" / "schedule_manager.pid"
    pidfile.parent.mkdir()
    _ = pidfile.write_text("1234")
    with serve_json(
        {"name": "schedule_manager", "home": str(home), "pid": 1234, "sha": sha}
    ) as server:
        with (home / ".env").open("a") as env_file:
            _ = env_file.write(
                f"MEIVA_SCHEDULE_MANAGER_HEALTH_PORT={server.url.rsplit(':', 1)[1]}\n"
            )
        result = _target_probe(home, "ready", target, "--only-service", "schedule-manager")

    assert result.returncode == 0, (result.stdout, result.stderr)
    receipt = receipt_from_output(result.stdout, target=target)
    state = receipt.components["schedule-manager"]
    if version == "target":
        assert state == "ready"
    else:
        assert state == f"running SHA {sha or 'unavailable'} differs from target {target}"


def test_pending_schema_migration_recovers_stopped_runners_when_one_stop_fails(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    for alias in cluster.hosts:
        receipt = DomainReceipt(
            cluster.new,
            "local" if alias == "gw" else "runner",
            "c" * 64,
            "e" * 64,
            "d" * 64,
            {"config": "ready", "schema": "migration-pending", "checkpoint": "ready"},
        )
        cluster.probe_replies[("prepare", alias)] = (0, receipt.line())
    cluster.stop_fails["lin"] = 1
    cluster.stop_fails_leaves_up.add("lin")

    assert run("run") == 1

    assert ("stop", "gw") not in cluster.effects
    assert ("oneshot", "mac") in cluster.effects
    assert all(host["up"] and host["head"] == cluster.old for host in cluster.hosts.values())
    assert "schema-changing rollout stopped before gateway" in capsys.readouterr().out


def test_roster_agreement_on_old_sha_cannot_certify_a_run(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.roster_reply = json.dumps(
        [
            {
                "name": name,
                "online": True,
                "identity_mismatch": False,
                "head_sha": cluster.old,
                "running_sha": cluster.old,
                "serve_agent_runner": alias != "gw",
            }
            for alias, name in cluster.names.items()
        ]
    )

    assert run("run") == PARTIAL_FAILURE_EXIT
    assert f"roster is serving {cluster.old}, expected target {cluster.new}" in (
        capsys.readouterr().out
    )
