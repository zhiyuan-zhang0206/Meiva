# meiva:note
# Custody of the one application root, against a scripted helper: who may claim the root in a home,
# what a helper that keeps its root elsewhere means, and how a dormant helper root is stopped when
# its IPC is gone.
#
# The driver's launch, readiness and reconcile paths are asserted in `test_root_driver.py`.

from __future__ import annotations

import os
import subprocess
from collections.abc import Sequence
from functools import partial
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS
from base.host.permissions_helper.client import PermissionsHelperError, RootStatus
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.paths import root_run_dir
from base.sessions.tests.scripted_helper import ScriptedHelper, serve
from cli.commands.lifecycle import root_driver as driver
from cli.commands.lifecycle.tests.root_control.snapshots import snapshot_with_root
from cli.commands.lifecycle.tests.root_control.support import (
    LAUNCHD_FACTS,
    custody_helper,
    refuse_launchctl,
    this_process,
)

pytestmark = pytest.mark.real_service_readiness_gate


# meiva:note
# ── custody ──────────────────────────────────────────────────────────────────


def test_the_custody_refusal_names_the_recovery(short_tmp: Path) -> None:
    me = this_process()
    keeper_of_another_root = ScriptedHelper(
        ping={
            "pong": True,
            "root_stop_intent_v1": True,
            "helper_shutdown_v1": True,
            "pid": os.getppid(),
        },
        root_status={"pid": me.pid + 1, "run_dir": str(root_run_dir(short_tmp))},
    )

    with pytest.raises(driver.RootDriverError) as refusal:
        driver.require_root_owner(
            snapshot_with_root(me), helper=serve(keeper_of_another_root), home=short_tmp
        )

    assert "outside this home's signed-helper custody" in str(refusal.value)
    assert "meiva start" in str(refusal.value)


def test_a_root_the_helper_started_in_this_home_is_in_custody(short_tmp: Path) -> None:
    me = this_process()

    driver.require_root_owner(
        snapshot_with_root(me), helper=serve(custody_helper(short_tmp, me)), home=short_tmp
    )


def test_a_helper_keeping_its_root_in_another_home_does_not_have_custody_here(
    short_tmp: Path, tmp_path: Path
) -> None:
    me = this_process()
    other_home = custody_helper(tmp_path, me)

    with pytest.raises(driver.RootDriverError, match="signed-helper custody"):
        driver.require_root_owner(snapshot_with_root(me), helper=serve(other_home), home=short_tmp)


def test_a_helper_lacking_the_retirement_protocol_has_no_custody_to_offer(short_tmp: Path) -> None:
    me = this_process()
    old_helper = ScriptedHelper(ping={"pong": True, "pid": os.getppid()})

    with pytest.raises(driver.RootDriverError, match="lacks lifecycle protocols"):
        driver.require_root_owner(snapshot_with_root(me), helper=serve(old_helper), home=short_tmp)


# meiva:note
# The dormant-root stop over a macOS account whose launchd the test answers (or refuses) through
# `run`.
_stop_dormant = partial(
    driver.stop_dormant_helper_root,
    float("inf"),
    facts=LAUNCHD_FACTS,
    run=refuse_launchctl,
    timers=SYSTEM_TIMERS,
)


def test_a_platform_without_a_helper_has_no_dormant_helper_root_to_stop(short_tmp: Path) -> None:
    _stop_dormant(helper=None, home=short_tmp)


def test_stop_without_root_ipc_cancels_pending_helper_restart(short_tmp: Path) -> None:
    keeper = {"stop_requested": False}

    def stop() -> RootStatus:
        keeper["stop_requested"] = True
        return {"state": "stopped", "seeded": True, "restarts": 1, "stop_requested": True}

    helper = ScriptedHelper(
        ping={"pong": True, "root_stop_intent_v1": True, "helper_shutdown_v1": True},
        root_status={"state": "backoff", "seeded": True, "restarts": 1, "stop_requested": False},
        stop_root=stop,
    )

    _stop_dormant(helper=serve(helper), home=short_tmp)

    assert keeper["stop_requested"]


def test_a_keeper_that_holds_a_root_without_ipc_is_not_stopped_blind(short_tmp: Path) -> None:
    helper = ScriptedHelper(
        root_status={
            "state": "running",
            "seeded": True,
            "restarts": 0,
            "stop_requested": False,
            "pid": 4,
        }
    )

    with pytest.raises(driver.RootDriverError, match="custody requires recovery"):
        _stop_dormant(helper=serve(helper), home=short_tmp)


@pytest.mark.parametrize("loaded", [False, True])
def test_unusable_helper_socket_requires_positive_native_absence(
    short_tmp: Path, loaded: bool
) -> None:
    # meiva:note
    # launchd's answer is the one fact no local resource can produce: whether the helper job is
    # loaded. `retirement_query` is that answer.
    def runner(
        cmd: Sequence[str],
        /,
        *,
        timeout: float,
        capture_output: bool = True,
    ) -> subprocess.CompletedProcess[bytes]:
        del timeout, capture_output
        assert cmd[2].startswith(f"gui/{LAUNCHD_FACTS.uid}/")
        if loaded:
            return subprocess.CompletedProcess(cmd, 0, b"state = spawn scheduled\n", b"")
        return subprocess.CompletedProcess(cmd, 113, b"", b"Could not find service")

    unavailable = serve(ScriptedHelper(root_status=PermissionsHelperError("AF_UNIX path too long")))

    if loaded:
        with pytest.raises(driver.RootDriverError, match="custody is unavailable"):
            _stop_dormant(helper=unavailable, home=short_tmp, run=runner)
    else:
        _stop_dormant(helper=unavailable, home=short_tmp, run=runner)
