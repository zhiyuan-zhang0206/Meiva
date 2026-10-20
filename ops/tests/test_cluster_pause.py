# meiva:note
# `is_paused` reads the host's posture row, or judges one the caller already read.

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.state import host_deploy_state
from base.deploy.state.host_deploy_state import HostDeployState
from ops.cluster_status import pause as cluster_pause


def _state(posture: str) -> HostDeployState:
    return HostDeployState(machine="runner-a", posture=posture, updated_at=datetime.now(UTC))


def test_is_paused_judges_a_pre_read_state_without_another_db_read(
    monkeypatch: pytest.MonkeyPatch, database: Database, *, machine_identity: MachineIdentity
) -> None:
    def _unexpected_read(_db: object) -> HostDeployState | None:
        raise AssertionError("is_paused re-read host deploy state")

    monkeypatch.setattr(host_deploy_state, "read", _unexpected_read)

    def _no_name() -> str:
        raise AssertionError("is_paused resolved the name for a pre-read state")

    assert cluster_pause.is_paused(database, _state("paused"), machine_name=_no_name) is True
    assert cluster_pause.is_paused(database, _state("idle"), machine_name=_no_name) is False
    assert cluster_pause.is_paused(database, None, machine_name=_no_name) is False


def test_is_paused_without_an_argument_still_reads_fresh(
    monkeypatch: pytest.MonkeyPatch, database: Database, *, machine_identity: MachineIdentity
) -> None:
    reads = 0

    def _read(_db: object, machine: str) -> HostDeployState:
        assert machine == machine_identity.name()
        nonlocal reads
        reads += 1
        return _state("paused")

    monkeypatch.setattr(host_deploy_state, "read", _read)

    assert cluster_pause.is_paused(database, machine_name=machine_identity.name) is True
    assert reads == 1
