# meiva:note
# The `status_probe` op answers with this host's own snapshot, assembled from its real resources
# (the throwaway database through a real pool, the home's pidfiles, the host's psutil reading).

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from psycopg_pool import ConnectionPool

from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.db import Database
from base.native_process.loaded_commit import LoadedCommit
from ops.cluster import operations as cluster
from ops.cluster_status import ClusterStatus


@pytest.fixture
def pool(db_url: str) -> Iterator[ConnectionPool]:
    pool: ConnectionPool = ConnectionPool(
        db_url, min_size=1, max_size=1, kwargs={"prepare_threshold": None}
    )
    with pool:
        yield pool


@pytest.mark.usefixtures("unit_home")
def test_cluster_status_op_returns_this_hosts_snapshot(
    database: Database,
    pool: ConnectionPool,
    tmp_path: Path,
    *,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(name="runner-a", role=frozenset({"agent-runner"}))
    image = LoadedCommit(source_root=tmp_path, sha="abc1234")

    status = cluster.cluster_status_op(database, pool, image=image, local_machine=machine_identity)

    assert status.machine_name == "runner-a"
    assert (status.serve_gateway, status.serve_agent_runner) == (False, True)
    assert status.running_sha == "abc1234"
    assert status.schema_mismatch is None
    assert status.agent_count == 0
    assert ClusterStatus.model_validate(status.model_dump(mode="json")) == status


@pytest.mark.usefixtures("unit_home")
def test_cluster_status_op_without_a_pool_reads_through_a_direct_connection(
    database: Database, tmp_path: Path, *, machine_identity: ConfiguredMachineIdentity
) -> None:
    machine_identity.set(name="runner-a", role=frozenset({"agent-runner"}))
    image = LoadedCommit(source_root=tmp_path, sha=None)

    status = cluster.cluster_status_op(database, image=image, local_machine=machine_identity)

    assert (status.machine_name, status.running_sha) == ("runner-a", None)
    assert status.paused_reason == "startup"
