# meiva:note
# Every fleet choice is the operator's: these tests prove the choices reach each host verbatim and
# that a missing, conflicting or invalid one is refused before any host is touched.
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest

from cli import fleet_update
from cli.tests.fleet_update.fleet_update_cluster import Cluster

_STOP_CHOICES = [
    "--close-shell-sessions",
    "--stop-browser",
    "--stop-extras",
    "--keep-service",
    "frontend",
    "--keep-service",
    "gate",
    "--mode",
    "force",
    "--stop-timeout",
    "77",
]


def test_the_operators_arguments_are_spliced_verbatim_into_each_hosts_stop_and_start(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert (
        run(
            "run",
            *_STOP_CHOICES,
            "--stop-infra",
            "--only-service",
            "gateway",
            "--only-service",
            "agent-host",
        )
        == 0
    )
    stops = {a: c for a, c in cluster.commands if 'meiva" stop' in c}
    verbatim = (
        'meiva" stop --yes --close-shell-sessions --stop-browser --stop-extras '
        "--keep-service frontend --keep-service gate --mode force --timeout 77"
    )
    assert set(stops) == {"gw", "mac", "lin"}
    assert all(verbatim in command for command in stops.values())
    assert "--stop-infra" in stops["gw"] and not any(
        "-infra" in stops[alias] for alias in ("mac", "lin")
    )
    starts = {
        a: c for a, c in cluster.commands if 'meiva" start' in c or "launchctl bootstrap" in c
    }
    only = "--only-service gateway --only-service agent-host"
    assert set(starts) == {"gw", "mac", "lin"}
    assert all(only in command for command in starts.values())
    assert "launchctl bootstrap" in starts["mac"] and "--all-services" not in starts["mac"]


def test_disabled_services_and_keeping_infra_are_forwarded_too(
    env: tuple[Cluster, Callable[..., int]],
) -> None:
    cluster, run = env
    assert run("run", "--keep-infra", "--disable-service", "browser") == 0
    gateway_stop = next(c for a, c in cluster.commands if a == "gw" and 'meiva" stop' in c)
    assert "--keep-infra" in gateway_stop and "--keep-no-services" in gateway_stop
    starts = [c for _, c in cluster.commands if 'meiva" start' in c or "launchctl bootstrap" in c]
    assert len(starts) == 3 and all("--disable-service browser" in c for c in starts)


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        (["--keep-service", "pty-sessions"], "--keep-service pty-sessions: it has its own"),
        (["--keep-service", "browser"], "--keep-service browser: it has its own"),
        (["--keep-service", "no-such-service"], "unknown service(s): no-such-service"),
        (["--disable-service", "no-such-service"], "unknown service(s): no-such-service"),
        (["--stop-timeout", "0"], "--stop-timeout 0: not a number > 0"),
        (["--start-timeout", "-5"], "--start-timeout -5: not a number > 0"),
        (["--silence-hours", "0"], "--silence-hours 0.0: not a number > 0"),
        (["--smoke-timeout", "0"], "--smoke-timeout 0: not a number > 0"),
        (["--roster-timeout", "-1"], "--roster-timeout -1: not a number > 0"),
        (["--lease-confirm-seconds", "-1"], "'-1' is not a number of seconds >= 0"),
        (["--mode", "gentle"], "--mode smooth|force"),
    ],
)
def test_a_value_a_host_would_refuse_is_refused_before_any_host(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    extra: list[str],
    message: str,
) -> None:
    cluster, run = env
    with pytest.raises(SystemExit) as exc:
        run("run", *extra)
    assert exc.value.code == 2
    assert message in capsys.readouterr().err
    assert cluster.commands == [] and cluster.effects == []


# meiva:note
# One answer per required choice of `run`, each paired with the text the refusal names it by; the
# test drops exactly one and expects only that one to be named.
_ONE_EACH = [
    (["--runner", "mac", "--runner", "lin"], "--runner ALIAS... | --no-runners"),
    (["--execute"], "--dry-run | --execute"),
    (["--all-services"], "--all-services | --only-service | --disable-service"),
    (["--start-timeout", "1800"], "--start-timeout"),
    (["--keep-shell-sessions"], "--keep-shell-sessions | --close-shell-sessions"),
    (["--keep-browser"], "--keep-browser | --stop-browser"),
    (["--keep-extras"], "--keep-extras | --stop-extras"),
    (["--keep-infra"], "--keep-infra | --stop-infra"),
    (["--keep-no-services"], "--keep-service | --keep-no-services"),
    (["--mode", "smooth"], "--mode"),
    (["--stop-timeout", "600"], "--stop-timeout"),
    (["--refuse-python-change"], "--allow-python-change | --refuse-python-change"),
    (["--refuse-red-schedules"], "--allow-red-schedules | --refuse-red-schedules"),
    (["--silence-hours", "4"], "--silence-hours"),
    (["--lease-confirm-seconds", "0"], "--lease-confirm-seconds"),
    (["--smoke-timeout", "300"], "--smoke-timeout"),
    (["--roster-timeout", "90"], "--roster-timeout"),
]


@pytest.mark.parametrize("dropped", range(len(_ONE_EACH)))
def test_each_single_missing_choice_is_refused_before_any_host(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    dropped: int,
) -> None:
    cluster, _ = env
    given = [arg for i, (tokens, _form) in enumerate(_ONE_EACH) if i != dropped for arg in tokens]
    argv = ["run", "--gateway", "gw", "--log-dir", str(tmp_path / "logs"), "--new", "HEAD"]
    with pytest.raises(SystemExit) as exc:
        fleet_update.main([*argv, *given])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    listed = [line.strip() for line in err.splitlines()[1:] if line.strip()]
    assert len(listed) == 1 and _ONE_EACH[dropped][1] in listed[0], err
    assert cluster.commands == []


_EVERY_CHOICE = {
    "down": ["--new", "HEAD"],
    "up": [],
    "run": ["--new", "HEAD"],
}


@pytest.mark.parametrize("half", ["down", "up", "run"])
def test_missing_choices_are_all_listed_at_once_and_no_host_is_touched(
    env: tuple[Cluster, Callable[..., int]],
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    half: str,
) -> None:
    cluster, _ = env
    argv = [half, "--gateway", "gw", "--log-dir", str(tmp_path / "logs"), *_EVERY_CHOICE[half]]
    with pytest.raises(SystemExit) as exc:
        fleet_update.main(argv)
    assert exc.value.code == 2
    err = capsys.readouterr().err
    expected = [
        "--dry-run | --execute",
        "--runner ALIAS... | --no-runners",
        "--all-services | --only-service | --disable-service",
        "--start-timeout",
    ]
    if half != "up":
        expected += [
            "--keep-shell-sessions | --close-shell-sessions",
            "--keep-browser | --stop-browser",
            "--keep-extras | --stop-extras",
            "--keep-infra | --stop-infra",
            "--keep-service | --keep-no-services",
            "--mode",
            "--stop-timeout",
            "--allow-python-change | --refuse-python-change",
            "--allow-red-schedules | --refuse-red-schedules",
            "--silence-hours",
            "--lease-confirm-seconds",
        ]
    if half != "down":
        expected += ["--smoke-timeout", "--roster-timeout"]
    assert all(item in err for item in expected), err
    assert cluster.commands == [] and cluster.effects == []
    assert not (tmp_path / "logs").exists()


def test_conflicting_choices_are_refused_before_any_host(
    env: tuple[Cluster, Callable[..., int]], capsys: pytest.CaptureFixture[str]
) -> None:
    cluster, run = env
    with pytest.raises(SystemExit) as exc:
        run("down", "--keep-browser", "--stop-browser", "--no-runners", "--runner", "mac")
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "exactly one of --keep-browser | --stop-browser" in err
    assert "--runner ALIAS... | --no-runners" in err
    assert cluster.commands == []
