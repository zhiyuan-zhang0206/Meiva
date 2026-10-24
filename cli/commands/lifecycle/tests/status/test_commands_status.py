# meiva:note
# `meiva status` end to end: the facts the shell reads and the real probes it runs.
#
# What the report says and in which order is decided from values, and is asserted in
# `test_status_plan.py` and `test_status_render.py`. These tests run the whole command
# against real resources: a scripted root-control socket, a manifest on disk, a git
# checkout as the prod source, the throwaway Redis the suite provisions as this command's
# data plane, and the unreachable gateway address the test environment configures.

from __future__ import annotations

import dataclasses
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator
from pathlib import Path
from urllib.parse import urlsplit

import pytest
from pydantic import ValidationError

from base.cluster.machine import MachineIdentity, roles_from_flags
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.paths import meiva_home, root_manifests_path
from cli.commands import probe
from cli.commands._repo import repo_root
from cli.commands.converge import incomplete
from cli.commands.lifecycle.status import cmd_status, gateway_report_from_body
from cli.commands.lifecycle.status.gateway import GatewayReport
from cli.commands.lifecycle.status.roster import NOT_LAUNCHED_NOTE
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root
from cli.database import OperatorDatabaseFactory
from cli.database.runtime import CommandRuntime
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_fakes import sess
from cli.database.tests.command_inputs import command_runtime as command_runtime
from services.supervision.meiva_root_glue import manifests
from services.supervision.roster import build_services


# meiva:note
# One `meiva status` invocation's inputs, with a short home so a root socket fits.
@dataclasses.dataclass(frozen=True)
class _Status:
    command: CommandRuntime
    database_factory: OperatorDatabaseFactory
    # meiva:note
    # The port of the throwaway Redis this command's data plane names.
    redis_port: int

    def run(self, capsys: pytest.CaptureFixture[str], *, role: str | None = None) -> str:
        if role is not None:
            self.command.machine.set(role=role)
        capsys.readouterr()
        assert cmd_status(database_factory=self.database_factory, command=self.command) == 0
        return capsys.readouterr().out


@pytest.fixture
def status(
    redis_url: str,
    operator_database: OperatorDatabaseFactory,
    command_runtime: CommandRuntime,
    short_tmp: Path,
) -> _Status:
    # meiva:note
    # `redis_url` comes first so the command's configuration snapshot names the suite's
    # throwaway Redis as its data plane; nothing else on this host is dialled for it.
    port = urlsplit(redis_url).port
    assert port is not None
    return _Status(dataclasses.replace(command_runtime, home=short_tmp), operator_database, port)


def _row(out: str, service: str) -> str:
    return next(line for line in out.splitlines() if line.startswith(sess(service) + " "))


# meiva:note
# The root-mark and probe-mark columns of one service row.
def _marks(row: str) -> tuple[str, str]:
    cells = row.split()
    return cells[1], cells[2]


# meiva:note
# The root manifest a `meiva start` of just these services would have written.
def _write_launch_record(home: Path, *services: str) -> None:
    chosen = [spec for spec in build_services() if spec.session in services]
    manifest = manifests.build_manifest(
        capabilities=("gateway", "agent-runner"),
        specs=chosen,
        environments={spec.session: {} for spec in chosen},
    )
    manifests.write_manifest(root_manifests_path(home), manifest)


# meiva:note
# ─── a host's whole report ────────────────────────────────────────────────────


# meiva:note
# A fresh home: no root answers and no start has written a manifest yet.
def test_a_gateway_host_reports_its_services_data_plane_and_relay(
    status: _Status, capsys: pytest.CaptureFixture[str]
) -> None:
    out = status.run(capsys, role="gateway")

    # meiva:note
    # The gateway services, never the runner's.
    assert sess("gateway") in out
    assert sess("frontend") in out
    assert sess("ops") not in out
    # meiva:note
    # Before any start nothing was launched; a down root claims no running service
    # (diagnostic-first: it reads as an empty tree, not a crash).
    for service in ("gateway", "frontend"):
        row = _row(out, service)
        assert NOT_LAUNCHED_NOTE in row
        assert _marks(row)[0] == "✗"
    # meiva:note
    # The data plane comes from the command's own configuration.
    infra = out.split("infra (pg/redis):\n", 1)[1].split("\n\n", 1)[0]
    assert re.search(r"^  [✓✗] postgres \(", infra, re.MULTILINE)
    assert f"  ✓ redis (127.0.0.1:{status.redis_port})" in infra
    # meiva:note
    # The host-level relay must not disappear behind healthy service rows.
    relay = out.split("redis bridge (private-network ingress):\n", 1)[1].split("\n", 1)[0]
    assert relay.startswith("  ")
    assert relay.strip()
    assert "lgtm (observability backend):" not in out


def test_a_pure_agent_runner_reports_ops_and_skips_the_central_services(
    status: _Status, capsys: pytest.CaptureFixture[str]
) -> None:
    out = status.run(capsys, role="agent-runner")

    assert sess("ops") in out
    assert sess("gateway") not in out
    # meiva:note
    # A pure agent-runner owns no entry port and no local pg/redis.
    assert "infra (pg/redis): skipped (agent-runner uses central node)" in out
    assert "infra (pg/redis):\n" not in out
    assert "redis bridge (private-network ingress):" not in out
    assert "gate (fleet UI entry):" not in out
    assert "lgtm (observability backend):" not in out
    # meiva:note
    # Issue #1111: a meiva-browser the host cannot run is shown WITH its reason rather
    # than silently dropped, so `meiva status` (the first diagnostic command) is not blind
    # to the broken service. This host has no program to find, so the gate always has one.
    browser = _row(out, "browser")
    assert "-- skipped: " in browser
    assert NOT_LAUNCHED_NOTE not in browser


# meiva:note
# The host reading survives a deployment whose LGTM backend is down or was never
# deployed (issue #46: its history is Prometheus's; this is psutil, read live). A down
# gateway is a status signal, not a failure: `cmd_status` returns 0 and the local view
# ran first.
def test_the_report_ends_with_the_release_and_the_gateways_own_view(
    status: _Status, capsys: pytest.CaptureFixture[str]
) -> None:
    repo = repo_root()
    head = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"], check=True, capture_output=True, text=True
    ).stdout.strip()

    out = status.run(capsys, role="agent-runner")

    assert "host (live cpu/memory/disk):" in out
    assert re.search(r"cpu \d+%\s+memory \d+% \([\d.]+/[\d.]+ GB\)\s+disk \d+%", out)
    assert f"release: source checkout {repo} at {head[:7]}\n" in out
    tail = out.rstrip().splitlines()
    assert tail[-2] == "gateway cluster status (GET /api/cluster/status):"
    assert re.fullmatch(r"  ✗ gateway unreachable: .+", tail[-1])


def test_an_unresolved_role_lists_every_service_ungated(
    status: _Status, capsys: pytest.CaptureFixture[str]
) -> None:
    unresolved = MachineIdentity(
        name=lambda: "unit-test-machine",
        role=lambda: roles_from_flags(
            serve_gateway=False, serve_agent_runner=False, serve_observability_station=False
        ),
        host=lambda: "localhost",
        description=lambda: None,
    )
    command = dataclasses.replace(status.command, machine=unresolved)

    capsys.readouterr()
    assert cmd_status(database_factory=status.database_factory, command=command) == 0
    out = capsys.readouterr().out

    assert "roles = unknown" in out
    assert sess("gateway") in out
    assert sess("ops") in out
    # meiva:note
    # no capability gate ran
    assert "no off-box Redis ingress" not in out
    assert "infra (pg/redis):\n" in out


def test_the_designated_observability_host_reports_its_backends(
    status: _Status, capsys: pytest.CaptureFixture[str]
) -> None:
    out = status.run(capsys, role="agent-runner,observability-station")

    backends = out.split("lgtm (observability backend):\n", 1)[1].split("\n\n", 1)[0].splitlines()
    assert backends
    assert all(re.match(r"  \w+: \w+ \(.*\)$", line) for line in backends)


# meiva:note
# ─── what the home records ────────────────────────────────────────────────────


def test_a_service_the_last_start_did_not_launch_is_explained(
    status: _Status, capsys: pytest.CaptureFixture[str]
) -> None:
    _write_launch_record(status.command.home, "gateway")

    out = status.run(capsys, role="gateway")

    assert NOT_LAUNCHED_NOTE not in _row(out, "gateway")
    assert NOT_LAUNCHED_NOTE in _row(out, "frontend")


# meiva:note
# Service rows read root units; retired watchdogs have no runtime row. The entry
# service shares the root/identity readiness table with the app, with no section of its
# own.
def test_the_root_column_reads_each_units_state_from_the_root(
    status: _Status, capsys: pytest.CaptureFixture[str]
) -> None:
    units = [UnitRow("gateway"), UnitRow("frontend", state="stopped"), UnitRow("gate")]
    with scripted_root(status.command.home, units):
        out = status.run(capsys, role="gateway,agent-runner")

    assert _marks(_row(out, "gateway"))[0] == "✓"
    assert _marks(_row(out, "frontend"))[0] == "✗"
    gate = _row(out, "gate")
    assert _marks(gate)[0] == "✓"
    assert "(identity)" in gate
    assert "gate (fleet UI entry):" not in out
    names = {line.split()[0] for line in out.splitlines() if line.startswith("meiva-")}
    assert sess("gateway-watchdog") not in names
    assert sess("agent-runner-watchdog") not in names


def test_an_aborted_converge_is_reported_before_the_service_table(
    status: _Status, capsys: pytest.CaptureFixture[str]
) -> None:
    assert "converge:" not in status.run(capsys, role="agent-runner")
    incomplete.record_incomplete(
        status.command.home, step="permissions helper", error="the login keychain is locked"
    )

    out = status.run(capsys, role="agent-runner")

    marker = out.index("converge: INCOMPLETE ")
    assert out.index("[meiva status]") < marker < out.index("service ")
    assert "the login keychain is locked" in out


# meiva:note
# ─── prod source drift (any installed host) ───────────────────────────────────


@pytest.fixture
def prod_source() -> Iterator[Callable[[str], None]]:
    """Make `$MEIVA_HOME/source` a real checkout on `branch` (`DETACHED` leaves HEAD
    detached at its only commit), and remove it afterwards: the suite shares one home."""
    source = meiva_home() / "source"

    def make(branch: str) -> None:
        assert not source.exists()
        source.mkdir(parents=True)

        def git(*args: str) -> None:
            subprocess.run(["git", "-C", str(source), *args], check=True, capture_output=True)

        git("init", "-b", "main")
        git("config", "user.email", "t@t")
        git("config", "user.name", "t")
        (source / "f").write_text("x")
        git("add", ".")
        git("commit", "-m", "init")
        if branch == "DETACHED":
            git("checkout", "--detach", "HEAD")
        elif branch != "main":
            git("checkout", "-b", branch)

    yield make
    shutil.rmtree(source, ignore_errors=True)


def _drift_run(status: _Status, capsys: pytest.CaptureFixture[str]) -> str:
    # meiva:note
    # The prod source is `$MEIVA_HOME/source`: this command operates the suite's own home.
    at_home = dataclasses.replace(
        status, command=dataclasses.replace(status.command, home=meiva_home())
    )
    return at_home.run(capsys, role="agent-runner")


# meiva:note
# The 2026-06-01 incident: an agent developing in the prod tree instead of a worktree.
def test_a_prod_source_off_main_warns_that_the_next_restart_runs_unreviewed_code(
    status: _Status, capsys: pytest.CaptureFixture[str], prod_source: Callable[[str], None]
) -> None:
    prod_source("meiva-7/fix")

    out = _drift_run(status, capsys)

    assert "prod source" in out
    assert "meiva-7/fix" in out
    assert "un-reviewed" in out
    assert "do not `checkout main`" in out


# meiva:note
# Source-mode releases detach every checkout at the released commit, so the line is
# informational and steers away from `checkout main`.
def test_a_detached_prod_source_is_the_release_state_not_a_warning(
    status: _Status, capsys: pytest.CaptureFixture[str], prod_source: Callable[[str], None]
) -> None:
    prod_source("DETACHED")

    out = _drift_run(status, capsys)

    assert "detached at the released commit" in out
    assert "un-reviewed" not in out
    assert "Recover:" not in out


def test_a_prod_source_on_main_or_absent_prints_nothing_about_drift(
    status: _Status, capsys: pytest.CaptureFixture[str], prod_source: Callable[[str], None]
) -> None:
    assert "prod source" not in _drift_run(status, capsys)

    prod_source("main")

    assert "prod source" not in _drift_run(status, capsys)


# meiva:note
# ─── release and gateway view ─────────────────────────────────────────────────


def test_the_gateways_status_body_becomes_a_report() -> None:
    body: dict[str, object] = {
        "machine_name": "test-host",
        "serve_gateway": True,
        "serve_agent_runner": True,
        "serve_observability_station": True,
        "paused": True,
        "paused_reason": "startup",
    }

    assert gateway_report_from_body(body) == GatewayReport(
        machine_name="test-host",
        serves="gateway + agent-runner + observability-station",
        paused=True,
        paused_reason="startup",
    )


def test_a_pre_station_gateway_still_parses() -> None:
    body: dict[str, object] = {
        "machine_name": "old-host",
        "serve_gateway": True,
        "serve_agent_runner": False,
        "paused": False,
    }

    assert gateway_report_from_body(body) == GatewayReport(
        machine_name="old-host", serves="gateway", paused=False, paused_reason=None
    )


def test_a_body_that_is_not_a_cluster_status_raises() -> None:
    with pytest.raises(ValidationError):
        gateway_report_from_body({"machine_name": "no-capabilities"})


# meiva:note
# ─── the row itself (owned by `cli.commands.probe`) ───────────────────────────


# meiva:note
# A still-running session that is now gated reads as `✓ ... -- skipped: <reason>`:
# the marks reflect real liveness, the suffix the gate, surfacing the mismatch.
def test_a_live_unit_that_is_now_gated_reads_as_live_with_the_skip_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    spec = next(s for s in build_services() if s.session == "browser")

    probe.print_service_row(
        spec, 16, "no display (headless)", root_units={"browser": {"state": "running"}}
    )

    out = capsys.readouterr().out
    assert "✓" in out
    assert "skipped: no display" in out
