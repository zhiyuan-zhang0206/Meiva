# meiva:note
# One window from the operator's seat: `run`/`split` stop every host before they start any,
# name the phase that failed, and never repeat a half; what the dry run plans, what one log
# holds, and the alert silence the window opens before its first stop and closes after its up.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import pytest

from cli.fleet_update.plan.host_failures import PARTIAL_FAILURE_EXIT
from cli.tests.fleet_update.fleet_update_cluster import Cluster


@pytest.mark.parametrize("command", ["run", "split"])
def test_runners_stop_first_and_start_last(
    env: tuple[Cluster, Callable[..., int]], command: str
) -> None:
    cluster, run = env
    for step in ("down", "up") if command == "split" else ("run",):
        assert run(step) == 0
    kinds = [kind for kind, _ in cluster.effects if not kind.startswith("silence-")]
    assert [a for k, a in cluster.effects if k == "stop"] == ["mac", "lin", "gw"]
    assert kinds.index("stop") > kinds.index("fetch") and kinds.index("switch") > max(
        i for i, k in enumerate(kinds) if k == "stop"
    )
    assert [a for k, a in cluster.effects if k in ("start", "oneshot")] == ["gw", "mac", "lin"]
    assert kinds[-10:-4] == ["smoke"] * 3 + ["refresh"] * 3
    assert set(kinds[-4:]) == {"schedules-verify", "plugins-verify"}
    assert [a for k, a in cluster.effects if k == "refresh"] == ["gw", "mac", "lin"]
    assert all(h["up"] and h["head"] == cluster.new for h in cluster.hosts.values())
    assert not cluster.silence_open


@pytest.mark.parametrize("failing", [("stop", "mac"), ("switch", "lin")])
def test_run_does_not_start_after_a_failed_down(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    failing: tuple[str, str],
) -> None:
    cluster, run = env
    cluster.fail[failing] = 1
    assert run("run") == 1
    assert cluster.effects[-1] == failing
    assert not {"start", "oneshot", "smoke", "refresh"} & {k for k, _ in cluster.effects}
    output = capsys.readouterr().out
    assert "Failed phase: down" in output and "rerun `down`" in output
    assert cluster.silence_open


@pytest.mark.parametrize("failing", [("smoke", "gw"), ("plugins-verify", "lin")])
def test_run_reports_up_failure_without_repeating_down(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    failing: tuple[str, str],
) -> None:
    cluster, run = env
    cluster.fail[failing] = 1
    # meiva:note
    # A failed smoke or drift check is a partial up (3): the window did not refuse, and every
    # other host is still reported. Down never repeats, which is what this test is about.
    assert run("run") == 3
    assert [a for k, a in cluster.effects if k == "stop"] == ["mac", "lin", "gw"]
    assert [a for k, a in cluster.effects if k == "switch"] == ["gw", "mac", "lin"]
    output = capsys.readouterr().out
    assert "up incomplete" in output and "Failed phase" not in output
    assert not cluster.silence_open


# meiva:note
# A host whose converge aborted leaves the rest of the cluster running: the run
# names that host's step and reason, keeps going, and exits 3 — distinct from a
# refusal (2) and from a failure before any host started (1).
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def test_a_host_that_fails_to_start_does_not_stop_the_others(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("start", "lin")] = 1
    cluster.incomplete = {
        "lin": json.dumps(
            {
                # meiva:note
                # Inside this window's own span (the fixture session starts at 12:00:00Z): the
                # marker counts as evidence only there, so a stamp outside it is dropped.
                "at": "2026-10-24T12:00:01+00:00",
                "step": "permissions helper build + sign + load",
                "error": "the login keychain is not unlocked",
            }
        )
    }

    assert run("up") == PARTIAL_FAILURE_EXIT

    assert [a for k, a in cluster.effects if k in ("start", "oneshot")] == ["gw", "mac", "lin"]
    output = capsys.readouterr().out
    assert "lin failed at 'permissions helper build + sign + load'" in output
    assert "the login keychain is not unlocked" in output
    assert "up incomplete: 1 host(s) failed" in output
    assert not cluster.silence_open


def test_run_dry_run_plans_both_halves_without_mutation(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("run", "--dry-run") == 0
    assert cluster.effects == [("pre-verify", "gw")]
    assert all(h["up"] and h["head"] == cluster.old for h in cluster.hosts.values())
    output = capsys.readouterr().out
    assert "phase: down" in output and "phase: up" in output
    assert "stop --yes" in output and "launchctl bootstrap" in output
    assert "smoke MACHINE" in output and "plugins verify" in output


def test_run_uses_one_log_and_accepts_both_halves_options(
    env: tuple[Cluster, Callable[..., int]], tmp_path: Path
) -> None:
    cluster, run = env
    assert (
        run(
            "run",
            "--stop-timeout",
            "30",
            "--start-timeout",
            "60",
            "--smoke-timeout",
            "45",
            "--roster-timeout",
            "15",
            "--silence-hours",
            "2",
        )
        == 0
    )
    logs = list((tmp_path / "logs").glob("*.log"))
    assert len(logs) == 1 and logs[0].name.endswith("-run.log")
    output = logs[0].read_text()
    assert "--mode smooth --timeout 30" in output and " -ge 12 " in output
    assert "smoke gw 45" in output
    assert "down complete" in output and "up complete" in output
    assert "run `up` when ready" not in output
    assert cluster.silence_hours == 2


def test_a_path_with_no_ssh_fails_the_window_in_one_line_before_any_host(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.environ["PATH"] = "/nonexistent-bin"
    assert run("run") == 1
    out = capsys.readouterr().out
    assert "FAILED: gw: cannot run ssh:" in out and "Failed phase: down" in out
    assert "Traceback" not in out
    assert cluster.commands == []


def test_run_requires_a_target_before_contacting_hosts(
    env: tuple[Cluster, Callable[..., int]], tmp_path: Path
) -> None:
    cluster, _ = env
    with pytest.raises(SystemExit) as exc:
        cluster.invoke(["run", "--gateway", "gw", "--log-dir", str(tmp_path / "logs")])
    assert exc.value.code == 2
    assert cluster.commands == []


_STOP_RETRY = "retry `meiva stop` with the same arguments this `down` gave it"


def test_the_first_failure_stops_the_half(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.fail[("stop", "mac")] = 1
    assert run("down") == 1
    assert [k for k, _ in cluster.effects if k not in ("fetch", "pre-verify")] == [
        "silence-open",
        "stop",
    ]
    assert _STOP_RETRY in capsys.readouterr().out


def test_dry_run_changes_nothing(env: tuple[Cluster, Callable[..., int]]) -> None:
    cluster, run = env
    assert run("down", "--dry-run") == 0
    assert run("up", "--dry-run") == 0
    assert [k for k, _ in cluster.effects] == ["pre-verify"]


def test_the_log_carries_no_secret(
    env: tuple[Cluster, Callable[..., int]], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    cluster.output = "MEIVA_CLUSTER_SECRET=s3cret-a Bearer tok-b postgresql://u:pw-c@h/db"
    assert run("down") == 0
    logs = list((tmp_path / "logs").iterdir())
    written = "".join(path.read_text() for path in logs) + capsys.readouterr().out
    assert "<redacted>" in written
    assert not [secret for secret in ("s3cret-a", "tok-b", "pw-c") if secret in written]
    assert all(path.stat().st_mode & 0o077 == 0 for path in logs)


def test_the_window_is_one_silence_opened_before_the_first_stop_and_closed_after_up(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("down", "--silence-hours", "3") == 0
    kinds = [kind for kind, _ in cluster.effects]
    assert kinds.index("silence-open") < kinds.index("stop")
    assert cluster.silence_open and cluster.silence_hours == 3.0
    assert cluster.new[:12] in cluster.silence_comment
    assert run("up") == 0
    assert not cluster.silence_open
    assert [k for k, _ in cluster.effects if k.startswith("silence-")] == [
        "silence-open",
        "silence-close",
    ]
    assert cluster.effects[-1] == ("silence-close", "gw")


def test_a_partial_up_ends_the_planned_outage_silence(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    # meiva:note
    # Hosts that did come back healthy must not spend the rest of the window with their alerts
    # suppressed because one other host failed to verify.
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("schedules-verify", "gw")] = 1
    assert run("up") == 3
    assert not cluster.silence_open
    assert "silence-close" in {k for k, _ in cluster.effects}


@pytest.mark.parametrize(
    "reply",
    [
        "SILENCE failed: GET /api/x: URLError",
        "SILENCE skipped: no Grafana admin credential is configured",
    ],
)
def test_a_silence_that_cannot_open_warns_and_the_update_goes_on(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str], reply: str
) -> None:
    cluster, run = env
    cluster.silence_reply = reply
    assert run("down") == 0
    assert (
        f"WARNING: alert silence not open: {reply.removeprefix('SILENCE ')}"
        in capsys.readouterr().out
    )
    assert [a for k, a in cluster.effects if k == "stop"] == ["mac", "lin", "gw"]


def test_the_dry_run_lists_the_silence_without_opening_it(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down", "--dry-run") == 0
    assert " - open --hours 4 --comment " in capsys.readouterr().out
    assert not cluster.silence_open
    assert [k for k, _ in cluster.effects if k.startswith("silence-")] == []


# meiva:note
# The same run with a marker left over from an earlier wave: the failure is reported at the
# start with the start's own words, not with an older step's. This is the direction that keeps
# a stale record from sending the reader to a cause that was never this attempt's.
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md
def test_a_marker_from_an_earlier_wave_is_not_blamed_for_this_start(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    assert run("down") == 0
    cluster.fail[("start", "lin")] = 1
    cluster.incomplete = {
        "lin": json.dumps(
            {
                "at": "2026-10-18T10:02:33+00:00",
                "step": "permissions helper build + sign + load",
                "error": "the login keychain is not unlocked",
            }
        )
    }

    assert run("up") == PARTIAL_FAILURE_EXIT

    output = capsys.readouterr().out
    assert any(
        line.startswith("PARTIAL: lin") and "failed at 'start'" in line
        for line in output.splitlines()
    )
    attributed = [line for line in output.splitlines() if line.startswith("PARTIAL: lin")]
    assert attributed and "permissions helper" not in attributed[0]
