"""Native pooler diagnostics retain evidence without gaining repair authority.

The diagnostic reads the pooler through `base.cluster` only. It cannot start one: the start
(`ensure_pgbouncer`) lives in `cli`, and the `services must not import cli` contract in
`pyproject.toml` is what keeps a diagnostic from importing it.
"""

from pathlib import Path
from typing import cast

import pytest

from base.cluster import ClusterPorts
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.cluster.record import ClusterRecord
from services.supervision.meiva_root_glue import diagnostic_probes
from services.supervision.meiva_root_glue.diagnostic_probes import pgbouncer


@pytest.mark.parametrize(
    "loopback,public,expected",
    [(True, True, "alive"), (True, False, "down"), (False, False, "down")],
)
# meiva:note
# Pooler protocol probe requires both loopback and public authenticated listeners.
def test_pooler_protocol_requires_both_authenticated_listeners(
    loopback: bool,
    public: bool,
    expected: str,
    *,
    machine_identity: MachineIdentityReader,
) -> None:
    assert not hasattr(diagnostic_probes, "ownership")

    def _fake_get_record(_home: Path) -> ClusterRecord | None:
        return ClusterRecord(
            ports=cast("ClusterPorts", {"pgbouncer": 6432}), gateway_home="/x", created_at=""
        )

    admin_password = "pooler-admin-credential"  # noqa: S105

    def _fake_listener_reachable(port: int, password: str) -> bool:
        assert (port, password) == (6432, admin_password)
        return loopback

    def _fake_public_listener_reachable(*_a: object, machine: MachineIdentityReader) -> bool:
        assert machine is machine_identity
        return public

    assert (
        pgbouncer(
            machine=machine_identity,
            record_reader=_fake_get_record,
            admin_password_reader=lambda _home: admin_password,
            loopback_reachable=_fake_listener_reachable,
            public_reachable=_fake_public_listener_reachable,
        ).verdict.value
        == expected
    )


def test_unknown_pooler_record_is_unavailable(*, machine_identity: MachineIdentityReader) -> None:
    def _fake_get_record(_home: Path) -> ClusterRecord | None:
        return None

    assert (
        pgbouncer(machine=machine_identity, record_reader=_fake_get_record).verdict.value
        == "unavailable"
    )
