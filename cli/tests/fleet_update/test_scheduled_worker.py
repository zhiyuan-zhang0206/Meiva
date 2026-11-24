from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path

import pytest

from cli.fleet_update.choices import DownChoices, FleetArgs
from cli.fleet_update.plan.release_target import TargetRequest
from cli.fleet_update.scheduling.host_timer import save
from cli.tests.fleet_update.fleet_update_cluster import Cluster


def _record(cluster: Cluster, source: str) -> Path:
    args = FleetArgs(
        half="run",
        gateway="gw",
        runner=("mac", "lin"),
        log_dir=cluster.workdir / "logs",
        dry_run=False,
        selection="--all-services",
        named={},
        start_timeout=1800,
        roster_timeout=2,
        target_request=TargetRequest(target_mode="channel", channel="latest"),
        message_text="planned update",
        rebuild_extras=False,
        down=DownChoices(
            "--keep-shell-sessions",
            "--keep-browser",
            "--keep-extras",
            "--keep-infra",
            "--keep-no-services",
            "smooth",
            300,
            1,
            "--refuse-python-change",
            "--refuse-red-schedules",
        ),
        source=source,
    )
    path = cluster.workdir / "scheduled.json"
    save(
        path,
        {
            "operation_id": "scheduled",
            "run_at": "2000-01-01T00:00:00Z",
            "initiator": source,
            "new": cluster.new,
            "repo": str(cluster.repo),
            "arguments": asdict(args),
            "state": "pending",
        },
    )
    return path


@pytest.mark.parametrize("source", ["agent:9876", "user"])
def test_worker_preflight_refusal_notifies_only_agent_initiator_without_stopping(
    env: tuple[Cluster, Callable[..., int]],
    source: str,
) -> None:
    cluster, _ = env
    cluster.hosts["gw"]["dirty"] = 1
    rc, output = cluster.invoke_worker(_record(cluster, source))
    assert rc == 2, output
    assert not any(kind == "stop" for kind, _ in cluster.effects)
    assert len(cluster.messages) == (1 if source.startswith("agent:") else 0)
    if cluster.messages:
        target, message = cluster.messages[0]
        assert target == 9876
        assert "exit 2" in message and "failed" in message


def test_worker_busy_abort_notifies_initiator_once_and_running_peers_without_stopping(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, _ = env
    cluster.running_agents = [9876, 7]
    rc, output = cluster.invoke_worker(_record(cluster, "agent:9876"))
    assert rc == 2, output
    assert not any(kind == "stop" for kind, _ in cluster.effects)
    assert sorted(target for target, _ in cluster.messages) == [7, 9876]
    assert all("running agents [9876, 7]" in message for _, message in cluster.messages)


def test_worker_quiescent_success_retains_one_initiator_receipt(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, _ = env
    rc, output = cluster.invoke_worker(_record(cluster, "agent:9876"))
    assert rc == 0, output
    assert sum(kind == "stop" for kind, _ in cluster.effects) == 3
    assert len(cluster.messages) == 1
    assert cluster.messages[0][0] == 9876
    assert "exit 0" in cluster.messages[0][1] and "complete" in cluster.messages[0][1]
