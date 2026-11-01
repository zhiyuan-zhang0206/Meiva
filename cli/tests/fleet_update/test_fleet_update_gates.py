# meiva:note
# The checks a window refuses on, and the ones it reports instead: the stored-schedule and
# drift verifications, the preflight refusals, the stop journal a host writes, and the fact
# that no half reads a hold.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.host.env.agent_env import AGENT_ID_ENV
from cli.fleet_update.plan.host_commands import CLEAN
from cli.fleet_update.plan.host_failures import PARTIAL_FAILURE_EXIT
from cli.fleet_update.plan.release_target import (
    ReleaseCertificate,
    format_tag_message,
)
from cli.tests.fleet_update.fleet_update_cluster import Cluster, certify_commit, git


def test_drift_checks_run_after_the_smoke_and_the_refresh(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    assert run("up") == 0
    assert [a for k, a in cluster.effects if k == "schedules-verify"] == ["gw"]
    plugins = [a for k, a in cluster.effects if k == "plugins-verify"]
    assert len(plugins) == 3 and set(plugins) == {"gw", "mac", "lin"}
    kinds = [kind for kind, _ in cluster.effects]
    first_verify = min(
        i for i, k in enumerate(kinds) if k in ("schedules-verify", "plugins-verify")
    )
    assert first_verify > max(i for i, k in enumerate(kinds) if k in ("smoke", "refresh"))


@pytest.mark.parametrize(
    ("failing", "detail"),
    [
        (("schedules-verify", "gw"), "RED the schedules-verify detail from gw"),
        (("plugins-verify", "mac"), "RED the plugins-verify detail from mac"),
    ],
)
def test_a_red_drift_check_fails_up_with_its_detail(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    failing: tuple[str, str],
    detail: str,
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[failing] = 1
    assert run("up") == 3
    out = capsys.readouterr().out
    assert detail in out and f"matrix: {failing[1]} failed at" in out
    assert {"smoke", "refresh"} <= {k for k, _ in cluster.effects}
    plugins = [a for k, a in cluster.effects if k == "plugins-verify"]
    assert len(plugins) == 3 and set(plugins) == {"gw", "mac", "lin"}


def test_every_red_drift_check_is_reported_not_only_the_first(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("schedules-verify", "gw")] = 1
    cluster.fail[("plugins-verify", "lin")] = 1
    assert run("up") == 3
    lines = capsys.readouterr().out.splitlines()
    failed = [line for line in lines if "matrix:" in line and "failed at" in line]
    assert any("gw" in line and "schedules verify" in line for line in failed)
    assert any("lin" in line and "plugins verify" in line for line in failed)


_RED_ROW = (
    "RED id=12 name=hierarchy-worker missing=call-signature:L42 catch_up(): "
    "missing a required argument: 'triggers'"
)


def test_stored_schedules_are_checked_against_new_before_anything_stops(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down") == 0
    kinds = [kind for kind, _ in cluster.effects]
    assert kinds.count("pre-verify") == 1
    assert kinds.index("pre-verify") < kinds.index("stop")


@pytest.mark.parametrize("command", ["run", "down"])
def test_a_red_stored_schedule_refuses_down_before_any_stop(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], command: str
) -> None:
    cluster, run = env
    cluster.stored_schedules = (1, ["RESULT ts=t checked=13 green=12 red=1 rc=1", _RED_ROW])
    assert run(command) == 2
    out = capsys.readouterr().out
    assert _RED_ROW in out and "--allow-red-schedules" in out and "would crash-loop" in out
    assert {k for k, _ in cluster.effects} & {"stop", "switch"} == set()


def test_allow_red_schedules_proceeds_and_still_lists_the_rows(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stored_schedules = (1, [_RED_ROW])
    assert run("down", "--allow-red-schedules") == 0
    out = capsys.readouterr().out
    assert "WARNING: stored schedule scripts red" in out and _RED_ROW in out
    stops = [a for k, a in cluster.effects if k == "stop"]
    assert set(stops[:-1]) == {"mac", "lin"} and stops[-1] == "gw"


def test_a_check_that_could_not_run_refuses_like_a_red(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stored_schedules = (2, ["TOOL-ERROR OperationalError: connection refused"])
    assert run("down") == 2
    out = capsys.readouterr().out
    assert "unevaluable (check exited 2)" in out and "connection refused" in out
    assert {k for k, _ in cluster.effects} & {"stop", "switch"} == set()


def test_the_dry_run_of_down_reports_a_red_without_changing_anything(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.stored_schedules = (1, [_RED_ROW])
    assert run("down", "--dry-run") == 2
    assert _RED_ROW in capsys.readouterr().out
    assert [k for k, _ in cluster.effects] == ["pre-verify"]


def test_packages_refresh_reports_and_never_forces(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    _, run = env
    assert run("down") == 0
    assert run("up") == 0
    out = capsys.readouterr().out
    assert out.count("packages refresh [") == 3
    assert "packages refresh [mac] summary: applied 2, conflict 1" in out


def test_a_dirty_tree_is_shown_not_cleaned(tmp_path: Path) -> None:
    repo = tmp_path / "dirty"
    repo.mkdir()
    git(repo, "init", "-q", "--initial-branch=main")

    def check() -> subprocess.CompletedProcess[str]:
        argv = ["bash", "-c", CLEAN]
        return subprocess.run(argv, cwd=repo, capture_output=True, text=True, check=False)

    assert check().returncode == 0
    for n in range(25):
        (repo / f"stray-{n:02}.txt").write_text("x")
    dirty = check()
    assert dirty.returncode == 1
    assert dirty.stdout.count("?? stray-") == 20 and "?? stray-00.txt" in dirty.stdout
    assert "move them away (do not delete)" in dirty.stdout
    assert len(list(repo.glob("stray-*.txt"))) == 25


_UNSAFE: list[tuple[str, object]] = [("hook", "yes"), ("active", "yes"), ("dirty", 3)]


@pytest.mark.parametrize(("key", "value"), _UNSAFE)
def test_preflight_refuses(
    env: tuple[Cluster, Callable[..., int]], key: str, value: object
) -> None:
    cluster, run = env
    cluster.hosts["lin"][key] = value
    assert run("down") == 2
    assert cluster.effects == []


@pytest.mark.parametrize("command", ["run", "down", "up"])
def test_refuses_inside_an_meiva_agent(
    env: tuple[Cluster, Callable[..., int]], command: str
) -> None:
    cluster, run = env
    cluster.environ[AGENT_ID_ENV] = "7"
    assert run(command) == 2
    assert cluster.effects == []


def test_hosts_that_disagree_on_head_are_refused(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.hosts["lin"]["head"] = "deadbeefdeadbeef"
    assert run("down") == 2
    assert cluster.effects == []
    assert "hosts disagree on HEAD" in capsys.readouterr().out


def test_a_python_change_needs_the_flag(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env
    repo = cluster.repo
    (repo / ".python-version").write_text("3.12.13\n")
    git(repo, "commit", "-qam", "python")
    cluster.new = git(repo, "rev-parse", "HEAD")
    certify_commit(repo, cluster.new, version="v2026.10.24.120001")
    assert run("down") == 2 and cluster.effects == []
    assert run("down", "--allow-python-change") == 0


@pytest.mark.parametrize(
    "journal",
    [
        {"operation": "stop", "complete": False},
        {"operation": "stop", "complete": True, "result": {"rc": 1}},
        {"operation": "restart", "complete": True, "result": {"rc": 0}},
        None,
    ],
)
def test_down_judges_a_stop_by_the_hosts_journal_as_well_as_the_exit_code(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    journal: dict[str, Any] | None,
) -> None:
    cluster, run = env
    cluster.journal_after_stop["lin"] = journal

    assert run("down") == 1

    assert "stop exited 0 but its journal records no completed stop" in capsys.readouterr().out
    assert [alias for kind, alias in cluster.effects if kind == "switch"] == []


def test_down_and_up_never_read_a_hold(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env

    assert run("down") == 0
    assert run("up") == 0

    assert any("lifecycle-op.json" in command for _alias, command in cluster.commands)
    assert not any("status --json" in command for _alias, command in cluster.commands)


def test_up_judges_a_start_by_its_exit_code(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("start", "lin")] = 1

    assert run("up") == PARTIAL_FAILURE_EXIT

    assert "PARTIAL: lin failed" in capsys.readouterr().out
    assert all(cluster.hosts[alias]["up"] for alias in ("gw", "mac"))


@pytest.mark.parametrize("channel", ["stable", "latest"])
def test_channel_mode_switches_hosts_to_the_certified_channel_commit(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    channel: str,
) -> None:
    cluster, run = env
    assert run("down", "--channel", channel) == 0
    out = capsys.readouterr().out
    assert f"target: channel {channel} -> v2026.10.24.120000 @ {cluster.new[:12]}" in out
    assert all(h["head"] == cluster.new for h in cluster.hosts.values())


def test_release_mode_supports_exact_certified_rollback_and_hotfix_tags(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    repo = cluster.repo
    (repo / "uv.lock").write_text("hotfix-1\n")
    git(repo, "commit", "-qam", "hotfix-1")
    hotfix_sha = git(repo, "rev-parse", "HEAD")
    cluster.new = hotfix_sha
    certify_commit(
        repo,
        hotfix_sha,
        version="v2026.10.24.120000-hf1",
        channels=("stable",),
        hotfix_of="v2026.10.24.120000",
    )

    assert run("down", "--release", "v2026.10.24.120000-hf1") == 0
    out = capsys.readouterr().out
    assert f"target: release v2026.10.24.120000-hf1 @ {hotfix_sha[:12]}" in out
    assert all(h["head"] == hotfix_sha for h in cluster.hosts.values())


def test_uncertified_or_tampered_targets_refuse_exit_2_before_any_ssh_command(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    repo = cluster.repo
    unreleased_head = git(repo, "rev-parse", "HEAD")

    assert run("down", "--new", unreleased_head) == 2
    assert "carries no meiva-release/v1 release tag" in capsys.readouterr().out
    assert cluster.commands == [] and cluster.effects == []

    git(repo, "tag", "v2026.10.24.130000", unreleased_head)
    git(repo, "tag", "-a", "v2026.10.24.130001", "-m", "manual tag", unreleased_head)
    forged_cert = ReleaseCertificate(
        version="v2026.10.24.130002",
        sha=cluster.new,
        source_ref="refs/heads/main",
        gate_run_id="99",
        gate_run_url="https://github.com/example/Meiva/actions/runs/99",
        certified_at="2026-10-24T13:00:02Z",
    )
    git(
        repo,
        "tag",
        "-a",
        "v2026.10.24.130002",
        "-m",
        format_tag_message(forged_cert),
        unreleased_head,
    )

    for extra, expected in (
        (["--new", unreleased_head], "not peeled commit"),
        (["--release", "v2026.10.24.130000"], "not an annotated tag carrying a meiva-release/v1"),
        (["--release", "v2026.10.24.130001"], "lacks a valid meiva-release/v1 JSON certificate"),
        (["--release", "v2026.10.24.130002"], "not peeled commit"),
        (["--release", "v2026.10.24.195959"], "is not present in"),
    ):
        assert run("down", *extra) == 2
        out = capsys.readouterr().out
        assert expected in out, out
        assert cluster.commands == [] and cluster.effects == []


def test_track_branch_rolls_out_unreleased_origin_branch_with_explicit_notice(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    repo = cluster.repo
    (repo / "uv.lock").write_text("preview-1\n")
    git(repo, "commit", "-qam", "preview-1")
    first_preview = git(repo, "rev-parse", "HEAD")
    (repo / "uv.lock").write_text("preview-2\n")
    git(repo, "commit", "-qam", "preview-2")
    tip_preview = git(repo, "rev-parse", "HEAD")
    git(repo, "update-ref", "refs/remotes/origin/feat/preview", tip_preview)

    assert run("down", "--track-branch", "feat/missing") == 2
    assert "origin/feat/missing is not present" in capsys.readouterr().out
    assert cluster.commands == []

    git(repo, "checkout", "-q", "-b", "unrelated", cluster.old)
    (repo / "uv.lock").write_text("side\n")
    git(repo, "commit", "-qam", "side")
    unrelated_sha = git(repo, "rev-parse", "HEAD")
    git(repo, "checkout", "-q", "main")

    assert run("down", "--track-branch", "feat/preview", "--new", unrelated_sha) == 2
    assert "is not an ancestor of origin/feat/preview" in capsys.readouterr().out
    assert cluster.commands == []

    cluster.new = first_preview
    assert run("down", "--track-branch", "feat/preview", "--new", first_preview) == 0
    out = capsys.readouterr().out
    assert (
        f"target: UNRELEASED branch feat/preview @ {first_preview} (preview / rapid-iteration mode)"
        in out
    )
    assert all(h["head"] == first_preview for h in cluster.hosts.values())
