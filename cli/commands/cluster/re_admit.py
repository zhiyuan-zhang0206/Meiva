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

from base.host.boundary.json_values import is_object_dict, is_object_list
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
    if not response.get("ok") or not is_object_dict(result):
        print(f"  ✗ the root refused the re-admission: {result!r}", file=sys.stderr)
        return 1
    changed = result.get("changed")
    items: list[tuple[str, str, str]] = []
    for item in changed if is_object_list(changed) else []:
        if not is_object_list(item) or len(item) != 3:
            continue
        path, admitted, current = item
        if isinstance(path, str) and isinstance(admitted, str) and isinstance(current, str):
            items.append((path, admitted, current))
    print(f"re-admitted on {machine}: {unit} ({len(items)} changed input(s))")
    for path, admitted, current in items:
        print(f"  {path}: {admitted[:12]} -> {current[:12]}")
    if not items:
        print("  (nothing changed since admission; the seal already matches the contents)")
    print("  the running generation still uses what it started with; nothing was restarted")
    return 0
