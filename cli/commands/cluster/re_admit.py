# meiva:note
# `meiva cluster re-admit-inputs <unit>` — accept this unit's current declared-input contents for
# the rest of the running root generation (task #6403).
#
# The seal exists so a generation never spawns a process over configuration it did not admit. A
# converge that renders a sealed directory leaves every unit reading it unable to be replaced —
# the health monitor's own restart included — and the refusal only appears when something tries to
# spawn the unit. This is the operator's answer when the change was intended: it rewrites this
# generation's seals and restarts nothing, so a healthy unit keeps running what it started with
# and a down unit becomes replaceable again on the next health round.
#
# It acts on THIS home's root only, and every line it prints says so: a verb that reaches one
# machine must not read like a fleet-wide action.
# meiva:link docs/conventions/runbook.md
from __future__ import annotations

import sys
from collections.abc import Mapping
from pathlib import Path

from base.native_process.root_control.client import RootClient, RootClientError, caller_identity


# meiva:note
# Who asked, in the words the root records beside the exception it grants: the agent or operator,
# the pid, and the argv that named the unit.
def caller_summary(environ: Mapping[str, str], pid: int) -> str:
    caller = caller_identity(environ, pid=pid)
    argv = " ".join(caller.get("argv") or ())
    return f"{caller.get('agent_id') or 'operator'} pid={pid} argv={argv}"


def cmd_re_admit_inputs(unit: str, *, home: Path, environ: Mapping[str, str], pid: int) -> int:
    machine = str(caller_identity(environ, pid=pid).get("machine") or "this machine")
    client = RootClient(
        home / "run/meiva-root/meiva-root.sock",
        caller=caller_identity(environ, pid=pid),
        timeout=30.0,
    )
    try:
        response = client.resource(
            "admit-inputs", {"unit": unit, "caller": caller_summary(environ, pid)}
        )
    except RootClientError as error:
        print(f"  ✗ no root answers this home: {error}", file=sys.stderr)
        return 1
    result = response.get("result")
    if not response.get("ok") or not isinstance(result, dict):
        print(f"  ✗ the root refused the re-admission: {result!r}", file=sys.stderr)
        return 1
    changed = result.get("changed")
    items = (
        [item for item in changed if isinstance(item, list)] if isinstance(changed, list) else []
    )
    print(f"re-admitted on {machine}: {unit} ({len(items)} changed input(s))")
    for item in items:
        if len(item) == 3:
            path, admitted, current = item
            print(f"  {path}: {str(admitted)[:12]} -> {str(current)[:12]}")
    if not items:
        print("  (nothing changed since admission; the seal already matches the contents)")
    print("  the running generation still uses what it started with; nothing was restarted")
    return 0
