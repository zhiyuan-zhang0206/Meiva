# meiva:note
# `status_snapshot`: what one host reports about itself for `/api/cluster/status`.
#
# Every collaborator is the real one: the serving gate is the home's state file, the checkout
# sha is read from a real git checkout at `<home>/source`, the commit bookmark is the real file,
# and the supervisor is a real unix-socket server answering as the root.

from __future__ import annotations

from pathlib import Path

import pytest

from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import MachineIdentitySetter
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.deploy.git import running_sha
from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.tests.serving_root import serving_root as serving_root
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.native_process.loaded_commit import LoadedCommit
from base.paths.home import resolve_meiva_home
from base.paths.tests.homes import point_home
from gateway.tests.routers.cluster_endpoints.cluster_support import (
    prod_source_checkout,
    root_supervisor_socket,
)
from gateway.tests.routers.cluster_endpoints.cluster_support import (
    serving_gate as serving_gate,
)
from ops import cluster_status


def _snapshot(
    database: Database, machine: MachineIdentityReader, image: LoadedCommit
) -> cluster_status.ClusterStatus:
    return cluster_status.status_snapshot(database, image=image, local_machine=machine)


@pytest.mark.usefixtures("serving_gate")
def test_snapshot_reflects_the_serving_gate_and_role(
    set_machine_identity: MachineIdentitySetter,
    database: Database,
    tmp_path: Path,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    set_machine_identity(role="agent-runner", name="runner-a")
    image = LoadedCommit(source_root=tmp_path, sha=None)
    # meiva:note
    # unpaused
    snap = _snapshot(database, machine_identity, image)
    assert snap.machine_name == "runner-a"
    assert snap.serve_gateway is False
    assert snap.serve_agent_runner is True
    assert snap.paused is False
    # meiva:note
    # a stop clears the gate
    start_serving.clear_serving(resolve_meiva_home())
    snap2 = _snapshot(database, machine_identity, image)
    assert snap2.paused is True
    assert snap2.paused_reason == "startup"


# meiva:note
# The snapshot threads the prod-source HEAD so the roster can compare it against the cluster
# pin.
def test_snapshot_includes_head_sha(
    set_machine_identity: MachineIdentitySetter,
    unit_home: Path,
    database: Database,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    set_machine_identity(role="agent-runner", name="runner-a")
    head = prod_source_checkout(unit_home)
    snap = _snapshot(database, machine_identity, LoadedCommit(source_root=unit_home, sha=None))
    assert snap.head_sha == head


# meiva:note
# The snapshot threads the commit the answering process froze at its own boot, distinct from
# `head_sha` (the checkout the pin verdict compares) so the roster can expose a node running
# stale code even when its checkout reads on-pin. The supplied image decides: a checkout with a
# commit never fills in a sha the image does not carry.
@pytest.mark.parametrize("sha", ["def5678", None])
def test_snapshot_includes_running_sha(
    set_machine_identity: MachineIdentitySetter,
    unit_home: Path,
    database: Database,
    sha: str | None,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    set_machine_identity(role="agent-runner", name="runner-a")
    prod_source_checkout(unit_home)
    snap = _snapshot(
        database, machine_identity, LoadedCommit(source_root=unit_home / "source", sha=sha)
    )
    assert snap.running_sha == sha


# meiva:note
# A start that restarted nothing must not be able to make this node look current.
#
# `meiva start` writes `$MEIVA_HOME/running_sha` from the fresh HEAD one line before a launcher
# that skips already-running sessions, so after a no-op start the bookmark and the checkout agree
# while the live processes still hold the old code. Sourcing the snapshot from the bookmark made
# that state indistinguishable from a real restart: every roster consumer saw
# running_sha == head_sha for a node on days-old code. The snapshot answers from the process, so
# the stale commit survives the bookmark's advance and the divergence every drift renderer keys
# on is there.
def test_snapshot_ignores_the_start_bookmark(
    set_machine_identity: MachineIdentitySetter,
    unit_home: Path,
    database: Database,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    set_machine_identity(role="agent-runner", name="runner-a")
    head = prod_source_checkout(unit_home)
    running_sha.set(unit_home, head)

    snap = _snapshot(
        database,
        machine_identity,
        LoadedCommit(source_root=unit_home / "source", sha="0ld0ld0aaaa"),
    )

    assert snap.running_sha == "0ld0ld0aaaa"
    assert snap.head_sha == head
    assert snap.running_sha != snap.head_sha


# meiva:note
# A live root answering on the home's control socket reads as an online supervisor.
def test_supervisor_online_when_the_root_answers(
    set_machine_identity: MachineIdentitySetter,
    short_tmp: Path,
    monkeypatch: pytest.MonkeyPatch,
    database: Database,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    set_machine_identity(role="agent-runner", name="test-host")
    point_home(monkeypatch, short_tmp)
    with root_supervisor_socket(short_tmp):
        snap = _snapshot(database, machine_identity, LoadedCommit(source_root=short_tmp, sha=None))
    assert snap.supervisor_online is True


# meiva:note
# Inspection that cannot reach a root stays unknown: it is not reported as offline.
def test_supervisor_status_stays_unknown_when_no_root_listens(
    set_machine_identity: MachineIdentitySetter,
    unit_home: Path,
    database: Database,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    set_machine_identity(role="agent-runner", name="test-host")
    snap = _snapshot(database, machine_identity, LoadedCommit(source_root=unit_home, sha=None))
    assert snap.supervisor_online is None
