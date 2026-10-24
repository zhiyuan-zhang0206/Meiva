# meiva:note
# A start names who asked for it before it changes anything, so a bring-up that dies
# still leaves its request (task #5535). The orders are held with real failures: a manifest
# that cannot be published, an interpreter that cannot be run.

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from base.host.tests.short_tmp import short_tmp as short_tmp
from base.paths import root_manifests_path, root_run_dir
from cli.commands.lifecycle import root_driver as driver
from cli.commands.lifecycle.root_control.launch_plan import plan_root_launch
from cli.commands.lifecycle.tests.root_control.support import service
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root
from cli.database.lifecycle_audit import lifecycle_audit


# meiva:note
# The recorder of the command in progress: what it was told it was about to ask.
class Requests:
    def __init__(self) -> None:
        self.rows: list[tuple[str, list[str]]] = []

    def __call__(self, verb: str, units: Sequence[str]) -> None:
        self.rows.append((verb, list(units)))


def test_a_cold_start_records_its_units_before_the_manifest_is_published(
    short_tmp: Path,
) -> None:
    blocker = short_tmp / "not-a-directory"
    blocker.write_text("a file where the manifests' directory should be")
    launch = plan_root_launch(
        interpreter=Path("/unused"),
        cwd=short_tmp,
        run_dir=root_run_dir(short_tmp),
        manifests=blocker / "manifests.json",
    )
    requests = Requests()
    children: list[subprocess.Popen[bytes]] = []

    # meiva:note
    # the publication fails
    with lifecycle_audit(requests), pytest.raises(OSError):
        driver.bring_up_cold_root(
            (service(),),
            launch,
            {},
            driver.root_client(short_tmp),
            {},
            retained_children=children,
            helper=None,
        )

    assert requests.rows == [("up", ["gateway"])]
    # meiva:note
    # nothing was spawned
    assert children == []


def test_a_cold_start_records_its_units_and_publishes_the_manifest_before_the_root_is_spawned(
    short_tmp: Path,
) -> None:
    launch = plan_root_launch(
        interpreter=short_tmp / "no-such-interpreter",
        cwd=short_tmp,
        run_dir=root_run_dir(short_tmp),
        manifests=root_manifests_path(short_tmp),
    )
    requests = Requests()
    children: list[subprocess.Popen[bytes]] = []

    # meiva:note
    # the interpreter cannot be run
    with lifecycle_audit(requests), pytest.raises(OSError):
        driver.bring_up_cold_root(
            (service(),),
            launch,
            {"units": []},
            driver.root_client(short_tmp),
            {"PATH": os.environ["PATH"]},
            retained_children=children,
            helper=None,
        )

    assert requests.rows == [("up", ["gateway"])]
    assert launch.manifests.read_text().strip() == '{\n  "units": []\n}'
    assert children == []


def test_the_driver_records_the_units_before_it_asks_the_root(short_tmp: Path) -> None:
    requests = Requests()
    roster = (service(),)

    with scripted_root(short_tmp, [UnitRow("gateway", state="stopped")]) as root:
        client = driver.root_client(short_tmp)
        snapshot = driver.root_status(client)
        assert snapshot is not None
        asked_when_recorded: list[list[tuple[str, str | None]]] = []

        def record(verb: str, units: Sequence[str]) -> None:
            requests(verb, units)
            asked_when_recorded.append([call for call in root.calls if call[0] != "status"])

        with lifecycle_audit(record):
            driver.reconcile_units(roster, client, snapshot, honor_restart_budget=False)

    assert requests.rows == [("up", ["gateway"])]
    # meiva:note
    # the root had been asked nothing yet
    assert asked_when_recorded == [[]]
    assert root.units["gateway"].state == "running"


def test_a_stop_records_the_units_it_is_about_to_take_down(short_tmp: Path) -> None:
    requests = Requests()

    with (
        scripted_root(short_tmp, [UnitRow("agent-host"), UnitRow("gateway")]),
        lifecycle_audit(requests),
    ):
        driver.stop_root_service_tree(
            preserve=frozenset({"gateway"}),
            timeout_s=10.0,
            force=True,
            helper=None,
            home=short_tmp,
        )

    assert requests.rows == [("force-down", ["agent-host"])]
