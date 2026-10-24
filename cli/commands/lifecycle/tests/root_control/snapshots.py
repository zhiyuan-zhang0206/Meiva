# meiva:note
# Typed root snapshots for the root-control tests, built from the same rows the scripted
# root serves, so a plan test and a shell test read one shape.

from __future__ import annotations

from pathlib import Path

from base.native_process.ownership import OwnedProcess
from cli.commands.lifecycle.root_control.snapshot import RootSnapshot, parse_snapshot
from cli.commands.lifecycle.tests.scripted_root import ScriptedRoot, UnitRow

__all__ = ["snapshot_of", "snapshot_with_root"]

# meiva:note
# A snapshot reads only the tree's status, never a home.
_NO_HOME = Path("/nonexistent/snapshot-home")


# meiva:note
# The snapshot of a tree whose root is this test process.
def snapshot_of(*rows: UnitRow, launch_digest: str | None = None) -> RootSnapshot:
    tree = ScriptedRoot(
        home=_NO_HOME, units={row.unit_id: row for row in rows}, launch_digest=launch_digest
    )
    return parse_snapshot(tree.status())


# meiva:note
# The snapshot of a tree whose root is `root`, whatever process that names.
def snapshot_with_root(root: OwnedProcess, *rows: UnitRow) -> RootSnapshot:
    body = ScriptedRoot(home=_NO_HOME, units={row.unit_id: row for row in rows}).status()
    body["root"] = {
        "pid": root.pid,
        "create_time": root.birth,
        "starttime": root.starttime,
        "launch_digest": None,
    }
    return parse_snapshot(body)
