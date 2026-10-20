# meiva:note
# A failed stop must leave the cluster serving, not half stopped: these tests drive the stop phase
# to fail on one host and read back which hosts the window started again.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
from __future__ import annotations

from collections.abc import Callable

import pytest

from cli.tests.fleet_update.fleet_update_cluster import Cluster


# meiva:note
# Which hosts a run started, in order; the recovery's own starts are the only ones after a failed
# stop, so the list is the recovery.
def _started(cluster: Cluster) -> list[str]:
    return [alias for kind, alias in cluster.effects if kind in ("start", "oneshot")]


def test_a_failed_stop_brings_back_the_hosts_this_run_stopped(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_fails["lin"] = 1
    assert run("down") == 1
    out = capsys.readouterr().out
    assert _started(cluster) == ["mac", "lin"], "mac stopped cleanly, lin's stop failed; no gw"
    assert cluster.hosts["mac"]["up"] and cluster.hosts["lin"]["up"]
    assert cluster.hosts["gw"]["up"], "the gateway was never stopped"
    assert "recovered: mac, lin" in out
    assert "needs manual: none" in out
    assert "Failed phase: down" in out


def test_switch_never_runs_after_a_failed_stop(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    cluster.stop_fails["lin"] = 1
    assert run("down") == 1
    assert [kind for kind, _ in cluster.effects if kind == "switch"] == []
    assert all(host["head"] == cluster.old for host in cluster.hosts.values())


def test_a_failed_stop_that_leaves_the_daemon_running_starts_nothing_for_it(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_fails["lin"] = 1
    cluster.stop_fails_leaves_up.add("lin")
    assert run("down") == 1
    out = capsys.readouterr().out
    assert _started(cluster) == ["mac"]
    assert "needs manual: none" in out


def test_a_host_stopped_before_the_window_stays_stopped(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.hosts["mac"]["up"] = False
    cluster.hosts["mac"]["hold"] = ("paused", "stopped")
    cluster.host_up["mac"] = False
    cluster.stop_fails["lin"] = 1
    assert run("down") == 1
    assert _started(cluster) == ["lin"]
    assert "recovered: lin" in capsys.readouterr().out


def test_a_recovery_that_cannot_start_names_the_host(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_fails["lin"] = 1
    cluster.recover_fails.add("lin")
    assert run("down") == 1
    out = capsys.readouterr().out
    assert _started(cluster) == ["mac", "lin"]
    assert "recovered: mac" in out
    assert "needs manual: lin" in out
    assert not cluster.hosts["lin"]["up"]


def test_a_successful_down_starts_nothing(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    assert _started(cluster) == []
    assert all(not host["up"] for host in cluster.hosts.values())


def test_the_recovery_starts_with_the_operators_explicit_selection(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    cluster.stop_fails["lin"] = 1
    assert run("down", "--only-service", "gateway", "--only-service", "agent-host") == 1
    starts = [
        command
        for alias, command in cluster.commands
        if alias in ("mac", "lin")
        and ('meiva" start' in command or "launchctl bootstrap" in command)
    ]
    assert len(starts) == 2
    assert all("--only-service gateway --only-service agent-host" in command for command in starts)
