# meiva:note
# Bounded wait for the reachable (private-network) bind address before starting a daemon.
#
# On reboot brew/launchd can start `meiva` before the private network has assigned its address;
# binding to a not-yet-present address fails and takes the whole autostart down.
# `wait_for_reachable_bind` blocks (bounded) until the address is assigned and fails fast on
# timeout; a loopback-only single box never waits. The addresses here are real: loopback, the
# wildcard address (always bindable) and TEST-NET-1 (never assigned to an interface), so the
# verdicts come from the kernel; the retry-versus-expire decision is `test_bounded_wait.py`.

from __future__ import annotations

import time

import pytest

from base.clock import SYSTEM_TIMERS
from base.clock.tests.manual_timers import ManualTimers
from cli.commands.data_plane import cluster_instance as instance

# meiva:note
# 192.0.2.0/24 (RFC 5737 TEST-NET-1) is never assigned to a real interface, so binding it raises
# EADDRNOTAVAIL: the exact "address not up yet" signal.
_UNASSIGNED = "192.0.2.1"


def test_a_loopback_address_is_assigned() -> None:
    assert instance.addr_assigned("127.0.0.1") is True


def test_an_address_no_interface_holds_is_not_assigned() -> None:
    assert instance.addr_assigned(_UNASSIGNED) is False


@pytest.mark.parametrize("alias", ["localhost", "127.0.0.1", "::1", "ip6-localhost"])
def test_a_loopback_only_host_never_waits(alias: str) -> None:
    started = time.monotonic()
    assert (
        instance.wait_for_reachable_bind(host=alias, timeout_s=60.0, timers=SYSTEM_TIMERS) is True
    )
    assert time.monotonic() - started < 1.0


# meiva:note
# The wildcard address is bindable on every host and is not a loopback alias, so it is the
# assigned-address case without depending on this machine's interfaces (S104 names the address,
# which the test only probes and never listens on).
def test_an_address_the_kernel_can_bind_settles_on_the_first_look() -> None:
    started = time.monotonic()
    assert (
        instance.wait_for_reachable_bind(
            host="0.0.0.0",  # noqa: S104
            timeout_s=60.0,
            timers=SYSTEM_TIMERS,
        )
        is True
    )
    assert time.monotonic() - started < 1.0


# meiva:note
# The address is absent and the budget is spent: fail fast (the caller aborts the start).
def test_an_address_that_never_appears_gives_up_at_the_deadline() -> None:
    assert (
        instance.wait_for_reachable_bind(host=_UNASSIGNED, timeout_s=0.0, timers=SYSTEM_TIMERS)
        is False
    )


# meiva:note
# The wait spends the clock it was handed, not the process's: a manual clock that never waits can
# only reach the deadline through its own virtual sleeps, so a read of `SYSTEM_TIMERS` here would
# leave it unspent and run for the real timeout instead.
def test_the_bind_wait_spends_the_timers_it_was_handed() -> None:
    timers = ManualTimers(start=0.0)

    assert instance.wait_for_reachable_bind(host=_UNASSIGNED, timeout_s=4.0, timers=timers) is False
    assert timers.monotonic() >= 4.0
