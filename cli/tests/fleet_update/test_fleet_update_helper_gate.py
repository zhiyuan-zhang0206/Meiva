from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from cli import fleet_update
from cli.tests.fleet_update.fleet_update_cluster import Cluster

Env = tuple[Cluster, Callable[..., int]]


# meiva:note
# A host whose installed helper the target commit would have to rebuild, while that helper is
# still running: the window refuses before it stops anything, and says what to do. Staged on
# one host at a time so the gate cannot be right about the first machine only.
@pytest.mark.parametrize("staged", ["gw", "mac", "lin"])
def test_a_loaded_stale_helper_refuses_the_window_before_any_host_stops(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], staged: str
) -> None:
    cluster, run = env
    cluster.hosts[staged]["recorded_hash"] = "b" * 64
    cluster.hosts[staged]["loaded"] = "yes"
    assert run("down") == 2
    assert [kind for kind, _ in cluster.effects if kind == "stop"] == []
    out = capsys.readouterr().out
    for alias in cluster.hosts:
        assert f"{alias} recorded_hash=" in out
    assert "--rebuild-extras" in out and "retire extras" in out


def test_a_helper_every_host_already_carries_lets_the_window_run(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    assert [alias for kind, alias in cluster.effects if kind == "stop"] == ["mac", "lin", "gw"]


def test_a_helper_signed_by_another_certificate_refuses(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.hosts["mac"]["cert_root"] = "e" * 40
    assert run("down") == 2
    assert [kind for kind, _ in cluster.effects if kind == "stop"] == []
    assert "is not the recorded baseline" in capsys.readouterr().out


def test_a_host_without_a_baseline_is_refused_rather_than_skipped(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
) -> None:
    # meiva:note
    # The tracked table is what the gate reads, so a host missing from it is the state a new
    # machine arrives in: the window refuses instead of quietly skipping that host.
    cluster, run = env
    table = fleet_update.REPO / "deploy" / "fleet" / "helper-identities.json"
    table.write_text(json.dumps({"gw": "f" * 40, "mac": "f" * 40}))
    assert run("down") == 2
    assert [kind for kind, _ in cluster.effects if kind == "stop"] == []
    assert "no helper identity baseline recorded" in capsys.readouterr().out


def test_rebuild_extras_retires_extras_only_where_the_helper_is_stale(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    cluster.hosts["lin"]["recorded_hash"] = "b" * 64
    cluster.hosts["lin"]["loaded"] = "yes"
    assert run("down", "--stop-extras", "--rebuild-extras") == 0
    retiring = [c for alias, c in cluster.commands if alias == "lin" and '" stop' in c]
    others = [c for alias, c in cluster.commands if alias != "lin" and '" stop' in c]
    # meiva:note
    # Only the host whose helper must be rebuilt closes what the retirement needs closed; the
    # others keep the operator's shell and browser choices.
    assert retiring and "--close-shell-sessions" in retiring[0]
    assert retiring[0].count("--stop-browser") == 1
    assert all("--close-shell-sessions" not in command for command in others)


def test_rebuild_extras_excludes_keeping_extras(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env
    with pytest.raises(SystemExit) as refused:
        run("down", "--rebuild-extras")
    assert refused.value.code == 2
    assert cluster.effects == []
