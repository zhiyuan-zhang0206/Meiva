# meiva:note
# A failed gateway stop or switch must leave the cluster serving, not half stopped: these tests
# drive the gateway to fail and read back which hosts the window started again, and on which
# commit. (A runner's failure is a deferral, not a recovery: `test_fleet_update_deferral.py`.)
# meiva:link cli/docs/fleet-update-window/fleet-update-window.md
from __future__ import annotations

from collections.abc import Callable

import pytest

from cli.tests.fleet_update.fleet_update_cluster import Cluster


# meiva:note
# Which hosts a run started, in order; the recovery's own starts are the only ones after a failed
# stop, so the list is the recovery.
def _started(cluster: Cluster) -> list[str]:
    return [alias for kind, alias in cluster.effects if kind in ("start", "oneshot")]


def test_a_failed_gateway_stop_brings_back_the_runners_this_run_stopped(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_fails["gw"] = 1
    cluster.barriers["start"] = ["mac", "lin"]
    assert run("down") == 1
    out = capsys.readouterr().out
    started = _started(cluster)
    assert started[0] == "gw" and set(started[1:]) == {"mac", "lin"}
    assert all(cluster.hosts[alias]["up"] for alias in ("gw", "mac", "lin"))
    assert "recovered: gw, mac, lin" in out and "needs manual: none" in out
    assert "Failed phase: down" in out


def test_a_failed_gateway_stop_that_leaves_the_daemon_running_starts_only_the_runners(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_fails["gw"] = 1
    cluster.stop_fails_leaves_up.add("gw")
    assert run("down") == 1
    assert set(_started(cluster)) == {"mac", "lin"}
    assert "needs manual: none" in capsys.readouterr().out


# meiva:note
# Runners join the cluster through the gateway: when the gateway cannot be brought back, starting
# them would only fail on the connection, so the recovery stops at the gateway and names them.
def test_a_gateway_that_cannot_be_recovered_leaves_the_runners_alone(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_fails["gw"] = 1
    cluster.recover_fails.add("gw")
    assert run("down") == 1
    out = capsys.readouterr().out
    assert _started(cluster) == ["gw"]
    assert "recovered: none" in out
    assert "runners not started: not attempted: the gateway did not start" in out
    assert "needs manual: gw, mac, lin" in out


def test_switch_never_runs_after_a_failed_gateway_stop(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    cluster.stop_fails["gw"] = 1
    assert run("down") == 1
    assert [kind for kind, _ in cluster.effects if kind in ("switch", "rollback")] == []
    assert all(host["head"] == cluster.old for host in cluster.hosts.values())


def test_a_host_stopped_before_the_window_stays_stopped(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.hosts["mac"]["up"] = False
    cluster.host_up["mac"] = False
    cluster.journal["mac"] = {"operation": "stop", "complete": True, "result": {"rc": 0}}
    cluster.stop_fails["gw"] = 1
    assert run("down") == 1
    assert set(_started(cluster)) == {"gw", "lin"}
    assert "recovered: gw, lin" in capsys.readouterr().out


def test_a_recovery_that_cannot_start_names_the_host(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_fails["gw"] = 1
    cluster.recover_fails.add("lin")
    assert run("down") == 1
    out = capsys.readouterr().out
    assert set(_started(cluster)) == {"gw", "mac", "lin"}
    assert "recovered: gw, mac" in out and "needs manual: lin" in out
    assert not cluster.hosts["lin"]["up"]


def test_the_recovery_starts_with_the_operators_explicit_selection(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    cluster.stop_fails["gw"] = 1
    assert run("down", "--only-service", "gateway", "--only-service", "agent-host") == 1
    starts = [
        command
        for _alias, command in cluster.commands
        if 'meiva" start' in command or "launchctl bootstrap" in command
    ]
    assert len(starts) == 3
    assert all("--only-service gateway --only-service agent-host" in command for command in starts)


def test_an_unreadable_host_does_not_abort_recovery_or_replace_the_stop_failure(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_fails["gw"] = 1
    cluster.stop_fails_leaves_up.add("gw")
    cluster.fail["evidence", "gw"] = 255
    assert run("down") == 1
    out = capsys.readouterr().out
    assert set(_started(cluster)) == {"mac", "lin"}
    assert "recovery: could not read gw's evidence:" in out
    assert "recovered: mac, lin" in out and "needs manual: gw" in out
    assert "FAILED: gw: exit 1:" in out and "On that host retry `meiva stop`" in out
    assert [kind for kind, _ in cluster.effects if kind in ("switch", "rollback")] == []


def test_a_successful_down_starts_nothing(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    assert _started(cluster) == []
    assert all(not host["up"] for host in cluster.hosts.values())


def test_a_failed_gateway_switch_returns_every_host_to_its_old_commit_and_starts_it(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.fail[("switch", "gw")] = 1
    cluster.barriers["start"] = ["mac", "lin"]
    assert run("run") == 1
    out = capsys.readouterr().out
    assert {alias for kind, alias in cluster.effects if kind == "rollback"} == {"gw", "mac", "lin"}
    started = _started(cluster)
    assert started[0] == "gw" and set(started[1:]) == {"mac", "lin"}
    assert all(h["up"] and h["head"] == cluster.old for h in cluster.hosts.values())
    assert "recovered: gw, mac, lin" in out and "needs manual: none" in out
    assert "Failed phase: down" in out and "phase: up" not in out
    assert "refresh" not in {kind for kind, _ in cluster.effects}


def test_a_gateway_that_cannot_return_to_its_old_commit_leaves_the_runners_stopped(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.fail[("switch", "gw")] = 1
    cluster.fail[("rollback", "gw")] = 1
    assert run("run") == 1
    out = capsys.readouterr().out
    assert _started(cluster) == []
    assert "recovered: none" in out
    assert "runners not started: not attempted: the gateway did not start" in out
    assert "needs manual: gw, mac, lin" in out


def test_a_runner_that_cannot_return_to_its_old_commit_is_the_only_host_left_down(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.fail[("switch", "gw")] = 1
    cluster.fail[("rollback", "lin")] = 1
    assert run("run") == 1
    out = capsys.readouterr().out
    assert set(_started(cluster)) == {"gw", "mac"}
    assert "recovered: gw, mac" in out and "needs manual: lin" in out


def test_a_host_stopped_before_the_window_is_rolled_back_but_not_started_by_a_failed_switch(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    cluster.hosts["mac"]["up"] = False
    cluster.host_up["mac"] = False
    cluster.journal["mac"] = {"operation": "stop", "complete": True, "result": {"rc": 0}}
    cluster.fail[("switch", "gw")] = 1
    assert run("run") == 1
    assert set(_started(cluster)) == {"gw", "lin"}
    assert not cluster.hosts["mac"]["up"] and cluster.hosts["mac"]["head"] == cluster.old
