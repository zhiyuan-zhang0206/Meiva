# meiva:note
# Fresh protocol readiness of the requested services, read from a real control socket.

from __future__ import annotations

import os
from pathlib import Path

import pytest

from base.clock import SYSTEM_TIMERS
from base.daemon.health import DaemonProbe
from base.host.tests.short_tmp import short_tmp as short_tmp
from cli.commands.lifecycle import root_readiness as readiness
from cli.commands.lifecycle.tests.root_control.snapshots import snapshot_of
from cli.commands.lifecycle.tests.root_control.support import service
from cli.commands.lifecycle.tests.scripted_root import UnitRow, scripted_root

# meiva:note
# The readiness wait is the subject here, not the guard that stubs it for the start tests.
pytestmark = pytest.mark.real_service_readiness_gate

# meiva:note
# A wait that really sleeps between observations; short so a test that never settles fails fast.
POLL_S = 0.01
FAR_S = 60.0


def down() -> DaemonProbe:
    return DaemonProbe.down("unready")


def up() -> DaemonProbe:
    return DaemonProbe.up("ready")


def test_readiness_probes_now_instead_of_believing_the_roots_cached_health(
    short_tmp: Path,
) -> None:
    gateway = service(probe=down)

    with scripted_root(short_tmp, [UnitRow("gateway")]):
        result = readiness.wait_for_service_tree(
            (gateway,), timeout_s=0, home=short_tmp, timers=SYSTEM_TIMERS, environ=os.environ
        )

    assert result.unready == (gateway,)
    assert not result.sessions_gone


# meiva:note
# A root whose status cannot be read is no root yet: the poll keeps going and ends unready at its
# deadline instead of failing the whole start.
def test_a_root_with_an_unreadable_status_leaves_the_core_unready(short_tmp: Path) -> None:
    gateway = service(probe=up)

    with scripted_root(short_tmp, [UnitRow("gateway")]) as root:
        root.raw_unit_rows = [{"id": "gateway", "state": "running", "last_error": 5}]
        result = readiness.wait_for_service_tree(
            (gateway,),
            timeout_s=0.05,
            home=short_tmp,
            poll_interval_s=POLL_S,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
        )

    assert result.unready == (gateway,)
    assert not result.sessions_gone


def test_a_ready_core_settles_on_one_status_observation(short_tmp: Path) -> None:
    with scripted_root(short_tmp, [UnitRow("gateway")]) as root:
        result = readiness.wait_for_service_tree(
            (service(probe=up),),
            timeout_s=FAR_S,
            home=short_tmp,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
        )

    assert result.unready == ()
    assert [verb for verb, _ in root.calls] == ["status"]


def test_a_service_that_comes_up_during_the_wait_is_waited_for(short_tmp: Path) -> None:
    verdicts = iter([down(), down(), up()])

    with scripted_root(short_tmp, [UnitRow("gateway")]):
        result = readiness.wait_for_service_tree(
            (service(probe=lambda: next(verdicts)),),
            timeout_s=FAR_S,
            home=short_tmp,
            poll_interval_s=POLL_S,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
        )

    assert result.unready == ()
    assert result.elapsed_s >= 2 * POLL_S
    # meiva:note
    # all three answers were asked for
    assert next(verdicts, None) is None


def test_optional_failure_does_not_delay_a_ready_core(short_tmp: Path) -> None:
    core = service(probe=up)
    background = service("browser-mcp", probe=down)

    with scripted_root(short_tmp, [UnitRow("gateway"), UnitRow("browser-mcp", state="stopped")]):
        result = readiness.wait_for_service_tree(
            (core, background),
            timeout_s=FAR_S,
            home=short_tmp,
            poll_interval_s=POLL_S,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
        )

    assert result.unready == ()
    assert result.non_critical_unready == (background,)
    assert result.elapsed_s < FAR_S


def test_a_stopped_core_unit_is_not_ready_even_if_its_endpoint_answers(short_tmp: Path) -> None:
    gateway = service(probe=up)

    with scripted_root(short_tmp, [UnitRow("gateway", state="stopped")]):
        result = readiness.wait_for_service_tree(
            (gateway,), timeout_s=FAR_S, home=short_tmp, timers=SYSTEM_TIMERS, environ=os.environ
        )

    assert result.unready == (gateway,)
    assert result.sessions_gone
    assert result.elapsed_s < FAR_S


def test_missing_root_ipc_is_unknown_not_positive_exit(short_tmp: Path) -> None:
    gateway = service(probe=up)

    result = readiness.wait_for_service_tree(
        (gateway,),
        timeout_s=0.05,
        home=short_tmp,
        poll_interval_s=POLL_S,
        timers=SYSTEM_TIMERS,
        environ=os.environ,
    )

    assert result.unready == (gateway,)
    assert not result.sessions_gone
    assert result.elapsed_s >= 0.05


def test_a_spent_budget_still_makes_one_observation(short_tmp: Path) -> None:
    with scripted_root(short_tmp, [UnitRow("gateway")]) as root:
        result = readiness.wait_for_service_tree(
            (service(probe=up),),
            timeout_s=0,
            home=short_tmp,
            timers=SYSTEM_TIMERS,
            environ=os.environ,
        )

    assert result.unready == ()
    assert len(root.calls) == 1


def test_raising_identity_probe_reads_unavailable_and_warns() -> None:
    from base.log import logger

    def broken() -> DaemonProbe:
        raise ValueError("probe bug")

    records: list[str] = []
    sink = logger.add(lambda message: records.append(str(message)), level="WARNING")
    try:
        verdicts = readiness.health_verdicts(
            (service(probe=broken),), snapshot_of(UnitRow("gateway"))
        )
    finally:
        logger.remove(sink)

    assert verdicts == {"gateway": "unavailable"}
    # meiva:note
    # The sink is process-wide: count this probe's own report, not every warning
    # another producer emitted in the same window.
    reported = [record for record in records if "ValueError: probe bug" in record]
    assert len(reported) == 1


def test_no_snapshot_means_no_verdicts_and_no_probe_is_run() -> None:
    def refuse() -> DaemonProbe:
        raise AssertionError("an unreachable root must not be probed")

    assert readiness.health_verdicts((service(probe=refuse),), None) == {}
