# meiva:note
# What a wave reads on each host before it stops anything: the probe, and the helper facts of the
# commit it switches to. The decisions on what these reads refuse live in `plan/host_probe.py` and
# `plan/helper_gate.py`. Like the rest of `cli.fleet_update`, nothing here imports application
# code, so a window still runs from a bare operator checkout.
# meiva:link cli/docs/fleet-update.meiva.okf.md

from __future__ import annotations

import json
from pathlib import Path

from .plan.host_commands import PROBE, helper_probe_command
from .plan.host_probe import facts_of, was_serving
from .session import Session


# meiva:note
# One host's probe facts, and whether it was serving when the window began.
def probe_host(s: Session, alias: str) -> tuple[dict[str, str], bool]:
    facts = facts_of(s.run(alias, PROBE, effect=False))
    return facts, was_serving(facts)


def probe_helper(s: Session, alias: str, new: str) -> dict[str, str]:
    return facts_of(s.run(alias, helper_probe_command(new), effect=False))


# meiva:note
# The identity this fleet records for each host, read from the tracked table at call time: the
# table is data a pull request changes, never something a window writes.
def baselines(repo: Path) -> dict[str, str]:
    table: dict[str, str] = json.loads(
        (repo / "deploy" / "fleet" / "helper-identities.json").read_text()
    )
    return table
