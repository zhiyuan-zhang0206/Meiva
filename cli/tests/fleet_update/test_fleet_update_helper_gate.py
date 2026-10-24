from __future__ import annotations

import json
from collections.abc import Callable

import pytest

from base.paths import repo_root
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


def test_a_host_with_no_helper_is_no_blind_spot(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    # meiva:note
    # A Linux host carries no permissions helper at all, so `absence` is its whole answer: the
    # window runs, and only a host that must actually rebuild is retired for it. Reading the
    # missing facts as "could not tell" refuses every wave that has a Linux gateway.
    cluster, run = env
    cluster.hosts["gw"]["helper"] = "absent"
    cluster.hosts["mac"]["recorded_hash"] = "b" * 64
    cluster.hosts["mac"]["loaded"] = "yes"
    assert run("down", "--stop-extras", "--rebuild-extras") == 0
    retired = {
        alias
        for alias, command in cluster.commands
        if '" stop' in command and "--stop-browser" in command
    }
    assert retired == {"mac"}
    assert [alias for kind, alias in cluster.effects if kind == "stop"] == ["mac", "lin", "gw"]


def test_the_tracked_table_is_keyed_by_ssh_alias() -> None:
    # meiva:note
    # The gate looks each host up by the alias the window names (`mba`), never by the machine
    # name that host boots into (`macbook-air`): a table keyed by the other name tells every
    # window that the host has no baseline, so the two names must not drift apart again.
    table = json.loads((repo_root() / "deploy" / "fleet" / "helper-identities.json").read_text())
    assert set(table) == {"mba", "macmini", "company-air", "company-mini"}


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
    table = cluster.repo / "deploy" / "fleet" / "helper-identities.json"
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


def test_the_gate_runs_before_its_own_command_is_installed(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    # meiva:note
    # The first wave to carry this gate runs against a checkout that does not have the command
    # yet: the probe must come out of the target commit's objects, never out of the installed
    # checkout, or that wave could never deploy the gate that judges it.
    cluster, run = env
    assert run("down") == 0
    probes = [command for _alias, command in cluster.commands if "helper-signing.py" in command]
    assert probes
    for command in probes:
        assert 'git -C "$S" archive' in command and 'PYTHONPATH="$tmp"' in command
        assert '"$S/scripts/host_ops/tcc/helper-signing.py"' not in command
    assert [alias for kind, alias in cluster.effects if kind == "stop"] == ["mac", "lin", "gw"]


@pytest.mark.parametrize(
    "dropped", ["helper", "valid", "loaded", "stale", "cert_root", "expected_hash"]
)
def test_a_host_that_answers_short_refuses_the_window(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], dropped: str
) -> None:
    # meiva:note
    # A probe that could not run leaves a missing or `-` value. Reading that as "nothing to do"
    # is how a window walks past the one host it should have stopped for, so it refuses and
    # names the host and the field it owes.
    cluster, run = env
    cluster.hosts["mac"]["drop_fact"] = dropped
    assert run("down") == 2
    assert [kind for kind, _ in cluster.effects if kind == "stop"] == []
    out = capsys.readouterr().out
    assert "could not read the helper facts" in out
    assert "mac owes" in out and dropped in out


def test_a_probe_that_could_not_run_refuses_the_window(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.hosts["lin"]["facts_rc"] = 1
    assert run("down") == 2
    assert [kind for kind, _ in cluster.effects if kind == "stop"] == []
    assert "facts exited 1" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("error", "wanted"),
    [("archive", "archive could not be read"), ("unpack", "archive could not be unpacked")],
)
def test_a_target_commit_that_cannot_be_read_refuses_by_name(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    error: str,
    wanted: str,
) -> None:
    # meiva:note
    # The archive and its unpack have their own exit status, so a window says which step failed
    # instead of reporting the fingerprint as unreadable.
    cluster, run = env
    cluster.hosts["gw"]["probe_error"] = error
    assert run("down") == 2
    assert [kind for kind, _ in cluster.effects if kind == "stop"] == []
    out = capsys.readouterr().out
    assert wanted in out and "gw owes" in out


def test_a_rebuilt_gateway_keeps_its_data_plane_choice(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    # meiva:note
    # A rebuilding host gets the retirement leg, but that leg must not drop the gateway's own
    # `--keep-infra/--stop-infra`: the data plane is not the helper's business.
    cluster, run = env
    cluster.hosts["gw"]["recorded_hash"] = "b" * 64
    cluster.hosts["gw"]["loaded"] = "yes"
    assert run("down", "--stop-extras", "--rebuild-extras") == 0
    gateway = [c for alias, c in cluster.commands if alias == "gw" and '" stop' in c]
    assert gateway and "--keep-infra" in gateway[0]


@pytest.mark.parametrize("field", ["helper", "valid", "loaded", "stale"])
def test_unknown_helper_words_refuse_before_stop_or_switch(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], field: str
) -> None:
    cluster, run = env
    cluster.hosts["mac"][field] = "future"
    if field == "loaded":
        cluster.hosts["mac"]["recorded_hash"] = "b" * 64
    assert run("down") == 2
    assert all(kind == "fetch" for kind, _ in cluster.effects)
    out = capsys.readouterr().out
    assert "invalid helper probe: mac:" in out and field in out and "'future'" in out


@pytest.mark.parametrize("field", ["hook", "active"])
def test_unknown_host_flags_refuse_before_fetch(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], field: str
) -> None:
    cluster, run = env
    cluster.hosts["mac"][field] = "future"
    assert run("down") == 2
    assert cluster.effects == []
    out = capsys.readouterr().out
    assert f"invalid host probe: invalid mac.{field} probe value: 'future'" in out
