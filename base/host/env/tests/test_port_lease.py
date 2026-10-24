# meiva:note
# A leased port belongs to one live holder on this host, never to two.
#
# `base.host.env.port_lease` is the authority every test-side port number comes from:
# the session table `tests/fixtures/env_bootstrap.py` pins at import, the per-fixture
# draws of the data plane, and the free-port helper of each package's tests. Each of
# those used to ask the kernel for `:0` and close the sockets when its loop ended, so
# the same number could go to a sibling xdist worker — or, the incident these tests
# exist for, to a later fixture of the very same process, whose start-intent record
# then carried two slots on one port (`RuntimeError: duplicate start port
# reservation`).
#
# The promise is host-wide, so the tests that matter spawn processes.

from __future__ import annotations

import json
import secrets
import subprocess
import sys
from collections.abc import Callable, Mapping
from pathlib import Path

import pytest

from base.host.env import port_lease
from base.host.env.port_table import FIXED_PORTS

_REPO_ROOT = Path(__file__).resolve().parents[3]
_LEASE_CHILD = Path(__file__).with_name("_lease_child.py")


# meiva:note
# Every child here runs this interpreter on a fixed script with one integer: no
# shell, no interpolated text.
def _lease_in_children(count: int, processes: int) -> list[list[int]]:
    children = [
        subprocess.Popen(  # noqa: S603
            [sys.executable, str(_LEASE_CHILD), str(count)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=_REPO_ROOT,
        )
        for _ in range(processes)
    ]
    leased: list[list[int]] = []
    for child in children:
        out, err = child.communicate(timeout=120)
        assert child.returncode == 0, err
        leased.append(json.loads(out))
    return leased


def test_one_process_finds_its_earlier_leases_held(
    distinct_free_ports: Callable[[int], list[int]],
) -> None:
    first = distinct_free_ports(3)
    second = distinct_free_ports(3)
    assert len(set(first + second)) == 6
    assert port_lease.leased_ports()[-3:] == second


def test_concurrent_processes_never_share_a_leased_port(
    distinct_free_ports: Callable[[int], list[int]],
) -> None:
    mine = distinct_free_ports(20)
    leased = [port for child in _lease_in_children(count=60, processes=4) for port in child]
    assert len(leased) == 240
    assert len(set(leased)) == 240, "two concurrent holders were given the same port"
    assert not set(leased) & set(mine)


# meiva:note
# A holder's numbers are the registry's while it lives, and nobody's once it is gone.
# A SIGKILLed holder runs no cleanup, so the next read of the registry must reap its
# lease file (the kernel already dropped its flock) rather than remember it.
def test_a_lease_is_registered_while_its_holder_lives_and_reaped_after_it_dies() -> None:
    child = subprocess.Popen(  # noqa: S603
        [sys.executable, str(_LEASE_CHILD), "2"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
        cwd=_REPO_ROOT,
    )
    assert child.stdout is not None
    leased = json.loads(child.stdout.readline())
    assert len(leased) == 2
    assert (port_lease.registry_dir() / f"lease-{child.pid}.json").is_file()
    assert port_lease.live_holders().get(child.pid) == leased
    child.kill()
    child.wait(timeout=30)
    assert child.pid not in port_lease.live_holders(), "a dead holder still pins its numbers"
    assert not (port_lease.registry_dir() / f"lease-{child.pid}.json").is_file(), (
        "the dead holder's file must be reaped, not remembered"
    )


# meiva:note
# A lease file nobody holds is residue, whatever wrote it: the next read must drop it
# and stop counting the numbers it names. Asserted on the file rather than on a killed
# child, because a concurrent worker reaps a killed holder's file first (which is the
# behavior under test, not a race to assert against).
def test_an_abandoned_lease_file_is_reaped_and_stops_holding_its_numbers() -> None:
    residue = port_lease.registry_dir() / f"lease-999999-{secrets.token_hex(4)}.json"
    residue.write_text(json.dumps({"pid": 999_999, "ports": [port_lease.BAND_FIRST]}))
    holders = port_lease.live_holders()
    assert 999_999 not in holders, "an abandoned lease file still counts as a holder"
    assert not residue.is_file(), "the residue must be reaped, not remembered"


def test_the_session_table_and_a_fixture_draw_never_share_a_number(
    session_ports: Mapping[str, int], distinct_free_ports: Callable[[int], list[int]]
) -> None:
    drawn = distinct_free_ports(30)
    assert not set(session_ports.values()) & set(drawn)
    assert len(set(session_ports.values())) == len(session_ports)


# meiva:note
# A registry that cannot be created or written stops the run: the message names the
# directory and offers no way to turn the guarantee off.
def test_an_unusable_registry_stops_the_run(tmp_path: Path) -> None:
    blocked = tmp_path / "registry"
    blocked.write_text("not a directory")
    with pytest.raises(RuntimeError, match="no fallback"):
        port_lease.ensure_registry(blocked)


def test_the_band_sits_above_the_fixed_table() -> None:
    assert max(FIXED_PORTS.values()) < port_lease.BAND_FIRST


def test_the_band_sits_below_the_platforms_ephemeral_range() -> None:
    first = _kernel_ephemeral_first()
    assert first is not None, (
        "this platform reports no ephemeral port range, so the lease band's ceiling is"
        " unproven here — teach `_kernel_ephemeral_first` this platform before trusting it"
    )
    assert first > port_lease.BAND_LAST, (
        f"the lease band {port_lease.BAND_FIRST}-{port_lease.BAND_LAST} reaches into the "
        f"ephemeral range that starts at {first}: a `bind(':0')` could be handed a leased port"
    )


# meiva:note
# The first port of this platform's ephemeral range: `/proc` on Linux, `sysctl` on
# macOS, `None` where neither answers (the caller fails rather than skipping, because a
# skipped port contract reads green forever).
def _kernel_ephemeral_first() -> int | None:
    proc = Path("/proc/sys/net/ipv4/ip_local_port_range")
    if proc.is_file():
        return int(proc.read_text().split()[0])
    sysctl = subprocess.run(
        ["/usr/sbin/sysctl", "-n", "net.inet.ip.portrange.first"],
        capture_output=True,
        text=True,
        check=False,
    )
    return int(sysctl.stdout.strip()) if sysctl.returncode == 0 else None
