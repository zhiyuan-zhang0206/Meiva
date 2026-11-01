from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from cli.tests.fleet_update.fleet_update_cluster import Cluster, certify_commit, git

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
    _group("--message --no-message", "--no-message"),
)
_START: tuple[Group, ...] = (
    _group("--smoke-timeout", "--smoke-timeout", "300"),
    _group("--roster-timeout", "--roster-timeout", "90"),
)


@pytest.fixture
def env(tmp_path: Path) -> tuple[Cluster, Callable[..., int]]:
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
    workdir = tmp_path / "cluster"
    workdir.mkdir()
    cluster = Cluster(
        git(repo, "rev-parse", "HEAD~1"), git(repo, "rev-parse", "HEAD"), repo=repo, workdir=workdir
    )
    certify_commit(repo, cluster.new)
    # meiva:note
    # The shipped table names real machines; a test cluster has its own aliases, so the table
    # in this repository is written the way an operator's pull request would write it.
    table = repo / "deploy" / "fleet" / "helper-identities.json"
    table.parent.mkdir(parents=True, exist_ok=True)
    table.write_text(json.dumps(dict.fromkeys(cluster.hosts, cluster.helper_cert_root)))
    git(repo, "add", "-A")
    git(repo, "commit", "-qm", "baselines")
    logs = str(tmp_path / "logs")
    target_flags = {"--channel", "--release", "--track-branch", "--new"}

    def run(half: str, *extra: str) -> int:
        # meiva:note
        # Run one half with a full explicit argv; a flag in `extra` replaces its group's default.
        given = set(extra)
        groups = (*_BOTH, *(_STOP if half != "up" else ()), *(_START if half != "down" else ()))
        chosen = [token for flags, tokens in groups if not given & flags for token in tokens]
        new = (
            ["--new", cluster.new] if half in ("run", "down") and not (given & target_flags) else []
        )
        argv = [half, "--gateway", "gw", "--log-dir", logs, *new, *chosen, *extra]
        return cluster.invoke(argv)

    return cluster, run
