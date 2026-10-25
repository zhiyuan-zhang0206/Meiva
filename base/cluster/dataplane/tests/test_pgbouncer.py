"""PgBouncer probes whose contract is independent of bring-up sequencing."""

from __future__ import annotations

import socket
from pathlib import Path

import pytest

from base.cluster.dataplane import pooler as base_pooler
from base.cluster.listener_plan import public_listener_bound
from base.cluster.machine import MachineFacts
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.port_preflight import bind_addrs
from base.config import ConfigBoot
from base.paths.tests.homes import point_home


@pytest.fixture
def probe_machine() -> MachineFacts:
    return MachineFacts.of(name="pooler-probe", role="gateway", host="10.0.0.5")


def test_public_probe_is_a_noop_for_loopback_only_bind(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    probe_machine: MachineFacts,
) -> None:
    """A loopback-only pooler has no public listener, so the fast path calls nothing."""
    point_home(monkeypatch, tmp_path)
    (tmp_path / ".env").write_bytes(b"\xff")
    with pytest.raises(UnicodeDecodeError):
        ConfiguredMachineIdentity(ConfigBoot()).host()

    owner = ConfiguredMachineIdentity(ConfigBoot())
    owner.set(
        host="localhost",
        role=probe_machine.role(),
        description=probe_machine.description(),
    )

    assert (
        base_pooler.pgbouncer_public_listener_reachable(6433, "meiva_main", machine=owner) is True
    )
    with pytest.raises(UnicodeDecodeError):
        owner.name()


def test_public_probe_accepts_the_exact_reachable_host_in_the_socket_table(
    probe_machine: MachineFacts,
) -> None:
    """The configured address itself proves the public listener is bound."""
    assert (
        public_listener_bound(
            bind_addrs(machine=probe_machine),
            probe_machine.host(),
            {"127.0.0.1", "10.0.0.5"},
        )
        is True
    )
    # meiva:note
    # A wildcard bind in the socket table covers every host interface, including
    # the declared reachable address.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("0.0.0.0", 0))  # noqa: S104
        listener.listen(1)
        port = listener.getsockname()[1]
        assert (
            base_pooler.pgbouncer_public_listener_reachable(
                port, "meiva_main", machine=probe_machine
            )
            is True
        )


@pytest.mark.parametrize(
    ("addrs", "expected"),
    [
        ({"127.0.0.1"}, False),
        ({"0.0.0.0"}, True),  # noqa: S104 — socket-table wildcard fixture
        ({"::"}, True),
        ({"*"}, True),
        (set[str](), False),
    ],
)
def test_public_probe_interprets_socket_table_bindings(
    addrs: set[str],
    expected: bool,
    probe_machine: MachineFacts,
) -> None:
    """Only the reachable address or a wildcard covers the public front door;
    loopback-only and unknown (empty) results remain degraded."""
    assert (
        public_listener_bound(
            bind_addrs(machine=probe_machine),
            probe_machine.host(),
            addrs,
        )
        is expected
    )
