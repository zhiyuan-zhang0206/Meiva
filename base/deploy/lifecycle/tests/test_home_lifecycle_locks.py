from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from base.deploy.lifecycle import home_lifecycle_locks as state
from base.native_process.os_platform import LockTimeoutError


def test_a_bounded_wait_names_the_last_holder(unit_home: Path) -> None:
    lock = state.lifecycle_lock_path(unit_home)
    assert lock == unit_home / "deploy-state.lifecycle.lock"

    with state.resource_lock(home=unit_home, pid=os.getpid(), purpose="ops.pause"):
        resource = json.loads(state.holder_path(lock).read_text())
        assert resource["state"] == "held"
        assert resource["purpose"] == "ops.pause"
        assert resource["pid"] == os.getpid()

        with (
            pytest.raises(LockTimeoutError, match=r"within 0\.1s") as failure,
            state.resource_lock(home=unit_home, pid=os.getpid(), purpose="cli.stop", timeout_s=0.1),
        ):
            pytest.fail("a concurrent stop entered a held resource section")
        assert "ops.pause" in str(failure.value)
        assert "cli.stop" in str(failure.value)

    released = json.loads(state.holder_path(lock).read_text())
    assert released["state"] == "released"
    assert released["purpose"] == "ops.pause"
