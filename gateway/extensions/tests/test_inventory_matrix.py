from datetime import UTC, datetime, timedelta
from typing import Any

from gateway.cluster.roster.liveness_snapshot import Snapshot
from gateway.extensions.inventory_matrix import collapse_inventory, plan_dials
from ops.rpc_schemas import InventoryReadResult


def _read(
    plugins: dict[str, Any], mcp_servers: dict[str, Any], machine: str
) -> InventoryReadResult:
    return InventoryReadResult(machine=machine, plugins=plugins, mcp_servers=mcp_servers)


def _plugin(*, enabled: bool, description: str = "") -> dict[str, Any]:
    return {"enabled": enabled, "can_enable": None, "reason": None, "description": description}


def _mcp(
    *, enabled: bool, can_enable: bool | None, reason: str | None, description: str = ""
) -> dict[str, Any]:
    return {
        "enabled": enabled,
        "can_enable": can_enable,
        "reason": reason,
        "description": description,
    }


def test_collapse_present_and_absent_cells() -> None:
    reads = {
        "A": _read({"X": _plugin(enabled=True, description="plugin X")}, {}, "A"),
        "B": _read({}, {}, "B"),
    }
    agg = collapse_inventory(reads, machines=["A", "B"], unreachable=[])

    assert agg.machines == ["A", "B"]
    assert agg.unreachable == []
    rows = {r.name: r for r in agg.plugins}
    x = rows["X"]
    assert x.kind == "plugin"
    assert x.description == "plugin X"
    assert x.hosts["A"].present is True
    assert x.hosts["A"].enabled is True
    assert x.hosts["B"].present is False
    assert x.hosts["B"].enabled is False


def test_collapse_excludes_unreachable_from_cells() -> None:
    reads = {"A": _read({"X": _plugin(enabled=True)}, {}, "A")}
    agg = collapse_inventory(reads, machines=["A", "B"], unreachable=["B"])

    assert agg.machines == ["A", "B"]
    assert agg.unreachable == ["B"]
    x = {r.name: r for r in agg.plugins}["X"]
    assert "B" not in x.hosts
    assert set(x.hosts) == {"A"}


def test_collapse_mcp_carries_capability_verdict() -> None:
    reads = {
        "A": _read({}, {"srv": _mcp(enabled=False, can_enable=False, reason="needs token")}, "A"),
    }
    agg = collapse_inventory(reads, machines=["A"], unreachable=[])
    srv = {r.name: r for r in agg.mcp_servers}["srv"]
    assert srv.kind == "mcp"
    assert srv.hosts["A"].enabled is False
    assert srv.hosts["A"].can_enable is False
    assert srv.hosts["A"].reason == "needs token"


_NOW = datetime(2026, 10, 2, 12, 0, tzinfo=UTC)


def _snapshot(*, failures: int, age_s: float = 5.0) -> Snapshot:
    return Snapshot(
        observed_at=_NOW - timedelta(seconds=age_s),
        reachable=failures == 0,
        consecutive_failures=failures,
        status=None,
        status_at=None,
    )


def test_plan_dials_skips_stopped_and_known_down_hosts() -> None:
    plan = plan_dials(
        [("A", False), ("B", True), ("C", False), ("D", False), ("E", False)],
        {
            # meiva:note
            # fresh, failed twice: known down
            "C": _snapshot(failures=2),
            # meiva:note
            # fresh, failed once: still dialed
            "D": _snapshot(failures=1),
            # meiva:note
            # stale: no evidence, dialed
            "E": _snapshot(failures=5, age_s=3600),
        },
        now=_NOW,
    )

    assert plan.to_dial == ("A", "D", "E")
    assert plan.skipped == ("B", "C")


def test_plan_dials_without_snapshots_dials_every_running_host() -> None:
    plan = plan_dials([("A", False), ("B", True)], {}, now=_NOW)

    assert (plan.to_dial, plan.skipped) == (("A",), ("B",))
