from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cli import fleet_update
from cli.tests.fleet_update.fleet_update_cluster import Cluster, git

Group = tuple[frozenset[str], tuple[str, ...]]


def _group(flags: str, *default: str) -> Group:
    return frozenset(flags.split()), default


# meiva:note
# Each group is the flags that mean one choice and the tokens that make it by default; a test
# passes an explicit choice for the one it is about and every other choice stays the same.
_BOTH: tuple[Group, ...] = (
    _group("--runner --no-runners", "--runner", "mac", "--runner", "lin"),
    _group("--dry-run --execute", "--execute"),
    _group("--all-services --only-service --disable-service", "--all-services"),
    _group("--start-timeout", "--start-timeout", "1800"),
)
_STOP: tuple[Group, ...] = (
    _group("--keep-shell-sessions --close-shell-sessions", "--keep-shell-sessions"),
    _group("--keep-browser --stop-browser", "--keep-browser"),
    _group("--keep-extras --stop-extras", "--keep-extras"),
    _group("--keep-infra --stop-infra", "--keep-infra"),
    _group("--keep-service --keep-no-services", "--keep-no-services"),
    _group("--mode", "--mode", "smooth"),
    _group("--stop-timeout", "--stop-timeout", "600"),
    _group("--allow-python-change --refuse-python-change", "--refuse-python-change"),
    _group("--allow-red-schedules --refuse-red-schedules", "--refuse-red-schedules"),
    _group("--silence-hours", "--silence-hours", "4"),
    _group("--lease-confirm-seconds", "--lease-confirm-seconds", "0"),
)
_START: tuple[Group, ...] = (
    _group("--smoke-timeout", "--smoke-timeout", "300"),
    _group("--roster-timeout", "--roster-timeout", "90"),
)


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Cluster, Callable[..., int]]:
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "--initial-branch=main")
    (repo / "base" / "sessions" / "pty").mkdir(parents=True)
    (repo / "base" / "sessions" / "pty" / "daemon.py").write_text("SESSIONS = 1\n")
    (repo / "services" / "supervision" / "healthchecks").mkdir(parents=True)
    (repo / "services" / "supervision" / "healthchecks" / "protocol_probe.py").write_text(
        "PROBE = 1\n"
    )
    for message in ("old", "new"):
        (repo / ".python-version").write_text("3.12.12\n")
        (repo / "uv.lock").write_text(message)
        git(repo, "add", "-A")
        git(repo, "commit", "-qm", message)
    cluster = Cluster(git(repo, "rev-parse", "HEAD~1"), git(repo, "rev-parse", "HEAD"))
    monkeypatch.setattr(fleet_update, "REPO", repo)
    monkeypatch.setattr(fleet_update, "ssh", cluster.ssh)
    monkeypatch.setattr(fleet_update, "time", cluster)
    monkeypatch.delenv("MEIVA_AGENT_ID", raising=False)
    logs = str(tmp_path / "logs")

    def run(half: str, *extra: str) -> int:
        # meiva:note
        # Run one half with a full explicit argv; a flag in `extra` replaces its group's default.
        given = set(extra)
        groups = (*_BOTH, *(_STOP if half != "up" else ()), *(_START if half != "down" else ()))
        chosen = [token for flags, tokens in groups if not given & flags for token in tokens]
        new = ["--new", cluster.new] if half in ("run", "down") else []
        cluster.run_started_at = len(cluster.effects)
        argv = [half, "--gateway", "gw", "--log-dir", logs, *new, *chosen, *extra]
        return fleet_update.main(argv)

    return cluster, run
