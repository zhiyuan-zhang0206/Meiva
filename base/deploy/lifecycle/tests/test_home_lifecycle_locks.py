from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from base.deploy.lifecycle import home_lifecycle_locks as state
from base.native_process.os_platform import LockTimeoutError

# meiva:note
# The pid a caller hands the lock: not this process, so a lock that read its own pid would record
# a different number.
_ENTRY_PID = os.getppid()

# meiva:note
# The instant the caller says it took the lock; the sidecar records it unchanged.
_NOW = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)


def test_a_bounded_wait_names_the_last_holder(unit_home: Path) -> None:
    lock = state.lifecycle_lock_path(unit_home)
    assert lock == unit_home / "deploy-state.lifecycle.lock"

    with state.resource_lock(home=unit_home, pid=_ENTRY_PID, purpose="ops.pause", now=_NOW):
        resource = json.loads(state.holder_path(lock).read_text())
        assert resource["state"] == "held"
        assert resource["since"] == _NOW.isoformat()
        assert resource["purpose"] == "ops.pause"
        assert resource["pid"] == _ENTRY_PID

        with (
            pytest.raises(LockTimeoutError, match=r"within 0\.1s") as failure,
            state.resource_lock(
                home=unit_home, pid=_ENTRY_PID, purpose="cli.stop", now=_NOW, timeout_s=0.1
            ),
        ):
            pytest.fail("a concurrent stop entered a held resource section")
        assert "ops.pause" in str(failure.value)
        assert "cli.stop" in str(failure.value)

    released = json.loads(state.holder_path(lock).read_text())
    assert released["state"] == "released"
    assert released["purpose"] == "ops.pause"
