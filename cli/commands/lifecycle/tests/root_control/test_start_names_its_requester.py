# meiva:note
# A start names who asked for it before it changes anything, so a bring-up that dies
# still leaves its request (task #5535). The orders are held with real failures: a manifest
# that cannot be published, an interpreter that cannot be run.

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.paths import root_manifests_path, root_run_dir
from cli.commands.lifecycle import root_driver as driver
from cli.commands.lifecycle.root_control.launch_plan import plan_root_launch
from cli.commands.lifecycle.tests.root_control.support import (
    LAUNCHD_FACTS,
    refuse_launchctl,
    service,
)
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root
from cli.database.tests.cli_tests import operator_database as operator_database
from cli.database.tests.command_inputs import command_runtime as command_runtime
from cli.database.tests.lifecycle_rows import LifecycleRows
from cli.database.tests.lifecycle_rows import lifecycle_rows as lifecycle_rows


def test_a_cold_start_records_its_units_before_the_manifest_is_published(
    short_tmp: Path, lifecycle_rows: LifecycleRows
) -> None:
    blocker = short_tmp / "not-a-directory"
    blocker.write_text("a file where the manifests' directory should be")
    launch = plan_root_launch(
        interpreter=Path("/unused"),
        cwd=short_tmp,
        run_dir=root_run_dir(short_tmp),
        manifests=blocker / "manifests.json",
    )
    children: list[subprocess.Popen[bytes]] = []

    # meiva:note
    # the publication fails
    with pytest.raises(OSError):
        driver.bring_up_cold_root(
            (service(),),
            launch,
            {},
            driver.root_client(short_tmp, environ=os.environ),
            {},
            retained_children=children,
            helper=None,
            timers=SYSTEM_TIMERS,
        )

    assert lifecycle_rows.requests() == [("up", ["gateway"])]
    # meiva:note
    # nothing was spawned
    assert children == []


def test_a_cold_start_records_its_units_and_publishes_the_manifest_before_the_root_is_spawned(
    short_tmp: Path, lifecycle_rows: LifecycleRows
) -> None:
    launch = plan_root_launch(
        interpreter=short_tmp / "no-such-interpreter",
        cwd=short_tmp,
        run_dir=root_run_dir(short_tmp),
        manifests=root_manifests_path(short_tmp),
    )
    children: list[subprocess.Popen[bytes]] = []

    # meiva:note
    # the interpreter cannot be run
    with pytest.raises(OSError):
        driver.bring_up_cold_root(
            (service(),),
            launch,
            {"units": []},
            driver.root_client(short_tmp, environ=os.environ),
            {"PATH": os.environ["PATH"]},
            retained_children=children,
            helper=None,
            timers=SYSTEM_TIMERS,
        )

    assert lifecycle_rows.requests() == [("up", ["gateway"])]
    assert launch.manifests.read_text().strip() == '{\n  "units": []\n}'
    assert children == []


def test_the_driver_records_the_units_before_it_asks_the_root(
    short_tmp: Path, lifecycle_rows: LifecycleRows
) -> None:
    roster = (service(),)

    with scripted_root(short_tmp, [UnitRow("gateway", state="stopped")]) as root:
        client = driver.root_client(short_tmp, environ=os.environ)
        snapshot = driver.root_status(client)
        assert snapshot is not None

        driver.reconcile_units(roster, client, snapshot, honor_restart_budget=False)

    assert lifecycle_rows.requests() == [("up", ["gateway"])]
    # meiva:note
    # The row was written, by the database's clock, before the root received the verb that
    # changes anything.
    (written,) = lifecycle_rows.written_at()
    assert written < root.asked_at[root.calls.index(("up", "gateway"))]
    assert root.units["gateway"].state == "running"


def test_a_stop_records_the_units_it_is_about_to_take_down(
    short_tmp: Path, lifecycle_rows: LifecycleRows
) -> None:
    with scripted_root(short_tmp, [UnitRow("agent-host"), UnitRow("gateway")]):
        driver.stop_root_service_tree(
            preserve=frozenset({"gateway"}),
            timeout_s=10.0,
            force=True,
            helper=None,
            home=short_tmp,
            facts=LAUNCHD_FACTS,
            run=refuse_launchctl,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
        )

    assert lifecycle_rows.requests() == [("force-down", ["agent-host"])]


# meiva:note
# The block the root records is the one the caller handed in: a client built with an agent id in
# its mapping names that agent, so the read is the shell's value and not this process's own.
def test_the_caller_block_names_the_environment_the_shell_handed_in(short_tmp: Path) -> None:
    with scripted_root(short_tmp, [UnitRow("gateway")]) as root:
        client = driver.root_client(
            short_tmp, environ={"MEIVA_AGENT_ID": "9", "MEIVA_OPERATION_ID": "op"}
        )
        client.down("gateway")

    assert root.callers, "the root recorded no caller block"
    assert root.callers[0]["agent_id"] == "9"
    assert root.callers[0]["operation_id"] == "op"
