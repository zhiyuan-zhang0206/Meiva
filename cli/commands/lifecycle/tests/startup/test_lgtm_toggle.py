# meiva:note
# `meiva lgtm on|off`: the station marker, then the operator's own start selection.
#
# The marker is a file in the home, so its presence is asserted on disk. The start the toggle drives
# is real: a root that already runs the requested generation is reused, and that generation covers
# the roster, so a start that reached the root with any other selection (or before the marker
# existed) would be refused as a changed generation.

from __future__ import annotations

import pytest

from base.telemetry.lgtm_local import BACKENDS
from cli.commands.lifecycle import lgtm_toggle as lgtm
from cli.commands.lifecycle.tests.scripted_root import UnitRow
from cli.commands.lifecycle.tests.startup.start_harness import StartRun, gateway_listener
from services.supervision.healthchecks.lgtm import lgtm_host_marker

GATEWAY = frozenset({"gateway"})
STATION = ("pty-sessions", *BACKENDS)


def _toggle(
    run: StartRun,
    which: str,
    *,
    only: tuple[str, ...] = (),
    disabled: tuple[str, ...] = (),
) -> int:
    command = lgtm.cmd_lgtm_on if which == "on" else lgtm.cmd_lgtm_off
    return command(
        only_services=only,
        disabled_services=disabled,
        all_services=False,
        runtime=run.runtime,
        retained_children=run.children,
        database_factory=run.database_factory,
        producer=run.producer,
        command=run.command,
    )


# meiva:note
# The units and launch digest of a root running exactly `sessions` from this checkout.
def _running(run: StartRun, *sessions: str) -> tuple[list[UnitRow], str]:
    roster = run.roster(GATEWAY, only=sessions)
    generation = run.generation(GATEWAY, roster)
    rows = [
        UnitRow(spec.session, manifest_digest=generation.units[spec.session]) for spec in roster
    ]
    return rows, generation.launch_digest


# meiva:note
# The backends are named, so the toggle accepts the selection and hands it, unchanged, to the
# start: the start resolves it against the roster and refuses the one name that is no service.
def test_on_declares_the_station_before_the_start_sees_the_operators_selection(
    start_run: StartRun,
) -> None:
    run = start_run
    run.declare(gateway=True, agent_runner=False)
    marker = lgtm_host_marker(run.home)

    with pytest.raises(ValueError, match=r"unknown service selection: typo$"):
        _toggle(run, "on", only=(*STATION, "typo"))

    assert marker.exists()
    assert not run.serving_published


def test_off_retracts_the_station_and_keeps_its_data(start_run: StartRun) -> None:
    run = start_run
    with gateway_listener() as gateway:
        run.declare(gateway=True, agent_runner=False, gateway_url=gateway.url)
        marker = lgtm_host_marker(run.home)
        marker.touch()
        data = marker.parent / "loki-data"
        data.write_bytes(b"durable history")
        rows, digest = _running(run, "pty-sessions")

        with run.root(rows, launch_digest=digest):
            assert _toggle(run, "off", only=("pty-sessions",)) == 0

    assert not marker.exists()
    assert data.read_bytes() == b"durable history"


def test_off_hands_the_operators_selection_to_the_start_unchanged(start_run: StartRun) -> None:
    run = start_run
    run.declare(gateway=True, agent_runner=False)
    marker = lgtm_host_marker(run.home)
    marker.touch()

    with pytest.raises(ValueError, match=r"unknown service selection: typo$"):
        _toggle(run, "off", only=("pty-sessions", "typo"))

    assert not marker.exists()


@pytest.mark.parametrize(
    ("which", "selection", "message"),
    [
        ("on", {"disabled": (BACKENDS[0],)}, "cannot --disable-service"),
        ("on", {"only": ("ops",)}, "must name every LGTM backend"),
        ("off", {"only": (BACKENDS[0],)}, "cannot --only-service"),
    ],
)
def test_a_selection_that_contradicts_the_toggle_is_refused_before_any_change(
    which: str, selection: dict[str, tuple[str, ...]], message: str, start_run: StartRun
) -> None:
    run = start_run
    marker = lgtm_host_marker(run.home)
    if which == "off":
        marker.touch()

    with pytest.raises(ValueError, match=message):
        _toggle(run, which, **selection)

    assert marker.exists() is (which == "off")
    assert not run.serving_published


# meiva:note
# The unit declared no capability, so the normal start refuses it; the toggle returns that
# refusal instead of reporting the station declared.
def test_a_start_the_toggle_drives_that_refuses_is_not_reported_as_success(
    start_run: StartRun,
) -> None:
    run = start_run

    assert _toggle(run, "on", only=STATION) == 1
