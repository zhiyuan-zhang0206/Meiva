"""Collapse validated host inventory into the cross-machine item matrix."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from gateway.cluster.roster.liveness_snapshot import Snapshot
from gateway.extensions.schemas import (
    InventoryAggregate,
    InventoryItemAggregate,
    InventoryItemHostState,
)
from ops.rpc_schemas import InventoryReadItem, InventoryReadResult


def collapse_inventory(
    reads: dict[str, InventoryReadResult],
    machines: list[str],
    unreachable: list[str],
) -> InventoryAggregate:
    """Collapse per-host inventory_read results into the cross-machine matrix.

    `reads` maps each REACHABLE machine name to its InventoryReadResult.
    `machines` is every name considered (column set), `unreachable` the subset
    whose read failed.

    For each item, the row's `description` is the first non-empty description
    seen across reachable hosts; a reachable host lacking the item gets a
    present=False cell. Unreachable hosts are excluded from every item's cells.
    """
    reachable = sorted(reads)

    def _rows(
        items_of: Callable[[InventoryReadResult], dict[str, InventoryReadItem]], kind: str
    ) -> list[InventoryItemAggregate]:
        names = sorted({name for m in reachable for name in items_of(reads[m])})
        rows: list[InventoryItemAggregate] = []
        for name in names:
            description = ""
            hosts: dict[str, InventoryItemHostState] = {}
            for m in reachable:
                item = items_of(reads[m]).get(name)
                if item is None:
                    hosts[m] = InventoryItemHostState(present=False, enabled=False)
                    continue
                if not description and item.description:
                    description = item.description
                hosts[m] = InventoryItemHostState(
                    present=True,
                    enabled=item.enabled,
                    can_enable=item.can_enable,
                    reason=item.reason,
                )
            rows.append(
                InventoryItemAggregate(name=name, kind=kind, description=description, hosts=hosts)
            )
        return rows

    return InventoryAggregate(
        machines=sorted(machines),
        unreachable=sorted(unreachable),
        plugins=_rows(lambda r: r.plugins, "plugin"),
        mcp_servers=_rows(lambda r: r.mcp_servers, "mcp"),
    )


# meiva:note
# Which agent-runners the aggregate dials, and which it reports unreachable unasked.
@dataclass(frozen=True)
class DialPlan:
    to_dial: tuple[str, ...]
    skipped: tuple[str, ...]


# meiva:note
# Decide which hosts the aggregate dials.
#
# `runners` is each agent-runner with its intentionally-stopped latch. A host that is
# stopped, or that the heartbeat liveness pass has just failed twice (`known_down`), is never
# dialed: it reports `unreachable` directly, because a dial could only hang (task #4127). The
# roster and the aggregate keep one liveness view, the pass's.
def plan_dials(
    runners: Sequence[tuple[str, bool]], snapshots: Mapping[str, Snapshot], *, now: datetime
) -> DialPlan:
    skipped = tuple(
        name
        for name, stopped in runners
        if stopped or (name in snapshots and snapshots[name].known_down(now))
    )
    return DialPlan(
        to_dial=tuple(name for name, _stopped in runners if name not in skipped), skipped=skipped
    )
