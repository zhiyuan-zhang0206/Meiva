from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from cli.fleet_update.plan.host_failures import PARTIAL_FAILURE_EXIT, RESULT_PREFIX
from cli.tests.fleet_update.fleet_update_cluster import Cluster


def _result(out: str) -> dict[str, list[dict[str, str]]]:
    lines = [line for line in out.splitlines() if line.startswith(RESULT_PREFIX)]
    assert len(lines) == 1, "exactly one machine-readable result line"
    record: dict[str, list[dict[str, str]]] = json.loads(lines[0][len(RESULT_PREFIX) :])
    return record


def _hosts_of(entries: list[dict[str, str]]) -> dict[str, str]:
    return {entry["host"]: entry["step"] for entry in entries}


def _started(cluster: Cluster) -> list[str]:
    return [alias for kind, alias in cluster.effects if kind in ("start", "oneshot")]


def _assert_healthy_hosts_serve_new(cluster: Cluster, *healthy: str) -> None:
    for alias in healthy:
        host = cluster.hosts[alias]
        assert host["up"] and host["head"] == cluster.new, alias
    assert not cluster.silence_open


def test_a_runner_that_goes_dark_at_switch_does_not_strand_the_cluster(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.die_at["lin"] = "switch"
    assert run("run") == PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    _assert_healthy_hosts_serve_new(cluster, "gw", "mac")
    assert _started(cluster)[0] == "gw" and set(_started(cluster)) == {"gw", "mac"}
    assert not cluster.hosts["lin"]["up"]
    result = _result(out)
    assert _hosts_of(result["deferred"]) == {"lin": "switch"} and result["failed"] == []
    assert "lin: exit 255:" in result["deferred"][0]["reason"]
    assert "DEFERRED: lin left out at 'switch'" in out
    assert "deferred: lin was left out of this update" in out
    assert "up complete: every listed machine except lin (deferred) runs" in out
    assert "matrix: lin" not in out and "Failed phase" not in out


def test_a_runner_whose_switch_fails_without_going_dark_is_deferred_the_same_way(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.fail[("switch", "lin")] = 1
    assert run("run") == PARTIAL_FAILURE_EXIT
    _assert_healthy_hosts_serve_new(cluster, "gw", "mac")
    assert _hosts_of(_result(capsys.readouterr().out)["deferred"]) == {"lin": "switch"}


def test_every_runner_failing_its_switch_still_brings_the_gateway_up(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.fail[("switch", "mac")] = 1
    cluster.fail[("switch", "lin")] = 1
    assert run("run") == PARTIAL_FAILURE_EXIT
    _assert_healthy_hosts_serve_new(cluster, "gw")
    assert _started(cluster) == ["gw"]
    assert _hosts_of(_result(capsys.readouterr().out)["deferred"]) == {
        "mac": "switch",
        "lin": "switch",
    }


@pytest.mark.parametrize("step", ["preflight", "fetch", "root-check", "prestage"])
def test_a_required_host_failure_refuses_the_whole_window_before_stop(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], step: str
) -> None:
    cluster, run = env
    if step == "preflight":
        cluster.dead = ["lin"]
    else:
        cluster.fail[(step, "lin")] = 255 if step == "root-check" else 1

    assert run("run") == 2

    output = capsys.readouterr().out
    assert "lin: exit" in output and "refused before stop" in output
    assert all(host["up"] and host["head"] == cluster.old for host in cluster.hosts.values())
    assert not cluster.silence_open
    assert not {"silence-open", "stop", "switch", "start", "oneshot"} & {
        kind for kind, _ in cluster.effects
    }


def test_a_runner_whose_stop_fails_is_deferred_and_the_others_update(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_fails["lin"] = 1
    cluster.stop_fails_leaves_up.add("lin")
    assert run("run") == PARTIAL_FAILURE_EXIT
    _assert_healthy_hosts_serve_new(cluster, "gw", "mac")
    assert cluster.hosts["lin"]["head"] == cluster.old
    assert ("switch", "lin") not in cluster.effects and ("start", "lin") not in cluster.effects
    assert _hosts_of(_result(capsys.readouterr().out)["deferred"]) == {"lin": "stop"}


def test_a_deferred_runner_is_left_alone_when_the_gateway_stop_then_fails(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stop_fails["lin"] = 1
    cluster.stop_fails["gw"] = 1
    assert run("down") == 1
    out = capsys.readouterr().out
    assert ("start", "lin") not in cluster.effects and ("oneshot", "lin") not in cluster.effects
    assert set(_started(cluster)) == {"gw", "mac"}
    assert "recovered: gw, mac" in out and "DEFERRED: lin left out at 'stop'" in out
    assert _hosts_of(_result(out)["deferred"]) == {"lin": "stop"}


def test_a_runner_that_goes_dark_at_start_leaves_the_others_serving_and_verified(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.die_at["lin"] = "start"
    assert run("run") == PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    _assert_healthy_hosts_serve_new(cluster, "gw", "mac")
    result = _result(out)
    assert result["deferred"] == []
    failed = {entry["host"] for entry in result["failed"]}
    assert "start" in {e["step"] for e in result["failed"] if e["host"] == "lin"}
    assert failed == {"lin"}
    assert "matrix: gw serving" in out and "matrix: mac serving" in out
    assert "failed at 'roster'" not in out


def test_a_split_down_reports_a_deferral_and_the_up_names_only_the_serving_runners(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.die_at["lin"] = "switch"
    assert run("down") == PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    assert "down complete: every host except lin is stopped" in out
    assert _hosts_of(_result(out)["deferred"]) == {"lin": "switch"}
    assert not any(h["up"] for h in cluster.hosts.values())
    before = [kind for kind, alias in cluster.effects if alias == "lin"]
    assert run("up", "--runner", "mac") == 0
    assert [kind for kind, alias in cluster.effects if alias == "lin"] == before
    _assert_healthy_hosts_serve_new(cluster, "gw", "mac")


def test_a_gateway_start_failure_keeps_the_cluster_stopped_with_recovery_instructions(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.fail[("start", "gw")] = 1
    assert run("run") == PARTIAL_FAILURE_EXIT
    out = capsys.readouterr().out
    assert _started(cluster) == ["gw"]
    assert "rollback" not in {kind for kind, _ in cluster.effects}
    assert all(h["head"] == cluster.new for h in cluster.hosts.values())
    assert "does not start the old version again" in out and "rerun `up`" in out
    assert "start" in {e["step"] for e in _result(out)["failed"] if e["host"] == "gw"}
    assert not cluster.silence_open
