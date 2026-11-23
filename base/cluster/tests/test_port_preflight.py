"""Socket-table listener discovery used by local infrastructure probes."""

from __future__ import annotations

import os
import socket

import pytest

from base.cluster import listener_plan, port_preflight
from base.cluster.machine.configured import ConfiguredMachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity


def test_listener_addrs_reads_all_matching_psutil_listeners() -> None:
    connections: list[tuple[str, tuple[object, ...], int | None]] = [
        ("LISTEN", ("0.0.0.0", 6433), None),  # noqa: S104 — socket-table wildcard fixture
        ("LISTEN", ("::", 6433), None),
        ("LISTEN", ("2001:db8::5", 6433), None),
        ("LISTEN", ("127.0.0.1", 6434), None),
        ("ESTABLISHED", ("10.0.0.72", 6433), None),
        ("LISTEN", (), None),
    ]

    assert listener_plan.parse_psutil_listener_addrs(6433, connections) == {
        "0.0.0.0",  # noqa: S104 — socket-table wildcard fixture
        "::",
        "2001:db8::5",
    }


def test_listener_addrs_falls_back_to_lsof_name_fields() -> None:
    stdout = (
        "p101\nn10.0.0.72:6433\n"
        "p102\nn*:6433\n"
        "p103\nn127.0.0.1:6433\n"
        "p104\nn192.0.2.1:6434\n"
        "p105\nn[::1]:6433\n"
    )
    assert listener_plan.parse_lsof_listener_addrs(6433, 0, stdout) == {
        "10.0.0.72",
        "*",
        "127.0.0.1",
        "::1",
    }


def test_listener_addrs_returns_empty_when_lsof_fails() -> None:
    assert listener_plan.parse_lsof_listener_addrs(6433, 1, "") == set()


def test_lsof_argv_prefers_path_then_absolute_candidates() -> None:
    """PATH resolves first; a restricted PATH lands on the candidate list."""
    assert listener_plan.resolve_lsof_argv(
        "-nP", path_binary="/opt/tools/lsof", executable_candidates=()
    ) == ["/opt/tools/lsof", "-nP"]


def test_lsof_argv_scans_candidates_in_order() -> None:
    assert listener_plan.resolve_lsof_argv(
        "-nP",
        path_binary=None,
        executable_candidates={"/usr/bin/lsof", "/sbin/lsof"},
    ) == ["/usr/bin/lsof", "-nP"]


def test_lsof_argv_none_when_lsof_is_nowhere() -> None:
    assert (
        listener_plan.resolve_lsof_argv("-nP", path_binary=None, executable_candidates=()) is None
    )


def test_strict_listeners_returns_conclusive_psutil_without_lsof() -> None:
    connections: list[tuple[str, tuple[object, ...], int | None]] = [
        ("LISTEN", ("0.0.0.0", 6433), 101),  # noqa: S104 — socket-table wildcard fixture
        ("LISTEN", ("::", 6433), 101),
        ("LISTEN", ("127.0.0.1", 6434), 102),
    ]
    assert listener_plan.reconcile_strict_listeners(6433, connections, []) == [101]


def test_strict_listeners_conclusive_absence_skips_lsof() -> None:
    """A table read that simply lacks the port proves absence — no lsof."""
    connections: list[tuple[str, tuple[object, ...], int | None]] = [
        ("LISTEN", ("127.0.0.1", 6434), 102)
    ]
    assert listener_plan.reconcile_strict_listeners(6433, connections, []) == []


def test_strict_listeners_falls_back_when_psutil_cannot_attribute() -> None:
    """A LISTEN row with pid=None is inconclusive, not proof of absence."""
    connections: list[tuple[str, tuple[object, ...], int | None]] = [
        ("LISTEN", ("0.0.0.0", 6433), None),  # noqa: S104 — socket-table wildcard fixture
        ("LISTEN", ("127.0.0.1", 6434), 102),
    ]
    lsof_pids = listener_plan.parse_lsof_pids(6433, 0, "p101\np101\n", "")
    assert listener_plan.reconcile_strict_listeners(6433, connections, lsof_pids) == [101]


def test_strict_listeners_raises_when_sightings_disagree() -> None:
    """psutil saw a listener; lsof says no match. Two inspections disagree,
    so absence is not established — this must raise, not report empty."""
    connections: list[tuple[str, tuple[object, ...], int | None]] = [
        ("LISTEN", ("0.0.0.0", 6433), None)  # noqa: S104 — socket-table wildcard fixture
    ]
    lsof_pids = listener_plan.parse_lsof_pids(6433, 1, "", "")
    with pytest.raises(port_preflight.ListenerDiscoveryError, match="neither psutil nor lsof"):
        listener_plan.reconcile_strict_listeners(6433, connections, lsof_pids)


def test_real_socket_listener_discovery() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        assert port_preflight.strict_listeners_on(port) == [os.getpid()]
        assert "127.0.0.1" in port_preflight.listener_addrs(port)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost", "ip6-localhost"])
def test_bind_addrs_adds_nothing_for_a_loopback_reachable_host(
    host: str,
    *,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(host=host)
    assert port_preflight.bind_addrs(machine=machine_identity) == ["127.0.0.1"]
    assert port_preflight.serves_off_box(machine=machine_identity) is False


def test_bind_addrs_includes_the_declared_reachable_address(
    *,
    machine_identity: ConfiguredMachineIdentity,
) -> None:
    machine_identity.set(host="10.0.0.5")
    assert port_preflight.bind_addrs(machine=machine_identity) == ["127.0.0.1", "10.0.0.5"]
    assert port_preflight.serves_off_box(machine=machine_identity) is True


def test_bind_addrs_ignores_the_ambient_cluster_secret(
    *, machine_identity: ConfiguredMachineIdentity
) -> None:
    machine_identity.set(host="localhost")
    assert port_preflight.bind_addrs(machine=machine_identity) == ["127.0.0.1"]


@pytest.mark.requires_host("linux")
def test_a_listener_on_another_loopback_address_does_not_overlap() -> None:
    with socket.socket() as neighbour:
        neighbour.bind(("127.0.0.2", 0))
        neighbour.listen()
        port = neighbour.getsockname()[1]
        assert port_preflight.listeners_at(listener_plan.TcpBind("127.0.0.1", port)) == []
        assert port_preflight.listeners_at(listener_plan.TcpBind("127.0.0.2", port)) == [
            (listener_plan.TcpBind("127.0.0.2", port), os.getpid())
        ]


@pytest.mark.requires_host("linux")
@pytest.mark.parametrize("ipv6_only", [0, 1])
def test_ipv6_wildcard_observes_the_kernels_ipv4_overlap(ipv6_only: int) -> None:
    with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as wildcard:
        wildcard.setsockopt(socket.IPPROTO_IPV6, socket.IPV6_V6ONLY, ipv6_only)
        wildcard.bind(("::", 0))
        wildcard.listen()
        port = wildcard.getsockname()[1]
        matches = port_preflight.listeners_at(listener_plan.TcpBind("127.0.0.1", port))
        assert matches == ([] if ipv6_only else [(listener_plan.TcpBind("::", port), os.getpid())])


def test_lsof_retains_the_family_and_pid_for_wildcard_sockets() -> None:
    binds = port_preflight.lsof_bound_sockets(
        9096, "p101\nf10\ntIPv6\nn*:9096\np102\nf11\ntIPv4\nn*:9096\n"
    )
    assert binds == [
        (listener_plan.TcpBind("::", 9096), 101),
        (listener_plan.TcpBind("0.0.0.0", 9096), 102),  # noqa: S104
    ]
