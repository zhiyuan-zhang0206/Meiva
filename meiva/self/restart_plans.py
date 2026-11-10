# meiva:note
# Where a restart goes and what it tells the caller, over values the `meiva.self` shell read: the
# harness this process runs under, the overlay's `harness` key and the harness stored for the
# agent. The shell (`meiva/self/__init__.py`) acts on the answers.
# meiva:link meiva/self/README.md

from __future__ import annotations

import json
from dataclasses import dataclass

from base.api_contracts.agent_contract import Harness, coerce_harness
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of


# meiva:note
# Where a restart goes, decided from the harness this process runs under and the overlay's
# `harness` key. A native agent that stays native writes its restart command itself, now, and ends
# its execution. Any other restart (the agent runs on an external harness, or asks to change
# harness) is validated by the gateway and recorded as a pending command that the host applies when
# the turn ends. `target` is the harness the overlay names, None when it names none; a name that is
# not a harness is refused here, before any write. `overlay` is what the route stores: the native
# route drops a `harness` key that only repeats the native harness. A controller attached through
# an impersonation lease registers no harness (it drives the agent from outside, and a restart it
# queued would wait on a turn loop nobody runs), so it is refused; a key that names no value is a
# mistake, not the default harness `coerce_harness` reads None as.
@dataclass(frozen=True, slots=True)
class RestartRoute:
    deferred: bool
    target: Harness | None
    overlay: dict[str, object] | None


def restart_route(*, current_harness: str, overlay: dict[str, object] | None) -> RestartRoute:
    if current_harness not in {harness.value for harness in Harness}:
        raise RuntimeError(
            "meiva.self.restart() is not available to an attached controller; restart the "
            "agent through meiva.agents.restart"
        )
    if not overlay or "harness" not in overlay:
        return RestartRoute(
            deferred=current_harness != Harness.MEIVA.value, target=None, overlay=overlay
        )
    named = overlay["harness"]
    if named is None:
        raise ValueError("unknown harness None: the overlay's harness must name a harness")
    target = coerce_harness(named)
    if current_harness == Harness.MEIVA.value and target is Harness.MEIVA:
        rest = {k: v for k, v in overlay.items() if k != "harness"}
        return RestartRoute(deferred=False, target=target, overlay=rest or None)
    return RestartRoute(deferred=True, target=target, overlay=overlay)


# meiva:note
# What a deferred restart tells the agent that asked: it was recorded, nothing has happened yet,
# and the turn it is in must end. A caller on an external harness reads this as the call's result
# (the native harness ends its execution instead), so it states the next step and what the agent
# will find on waking.
def deferred_restart_receipt(route: RestartRoute) -> str:
    where = f" and your harness becomes {route.target.value!r}" if route.target is not None else ""
    return (
        f"Restart accepted{where}. It is applied after the current turn ends, not now: finish "
        "the work this turn needs, then end the turn. Your next turn starts with a note carrying "
        "your message. Do not call restart again; a second request is refused while this one waits."
    )


# meiva:note
# The sentence a refused restart request gives the caller: the status and, when the body names
# one, the reason. The gateway's plain refusals answer `{"detail": <text>}`; any other body is
# shown as it came, so a refusal is never reduced to its status line.
def restart_refusal(status_code: int, body_text: str) -> str:
    try:
        body = object_of(json.loads, body_text)
    except ValueError:
        body = None
    detail = body.get("detail") if is_object_dict(body) else None
    shown = detail if isinstance(detail, str) else (body_text.strip() or "no reason given")
    return f"the restart was refused ({status_code}): {shown}"


# meiva:note
# The refusal for a native process whose agent the database stores on another harness: the native
# route would queue a restart that the other runtime applies, so the caller is told instead.
def stored_native_restart_refusal(row: tuple[object, ...] | None) -> str | None:
    if row is None or str(row[0]) == Harness.MEIVA.value:
        return None
    return (
        f"meiva.self.restart() runs natively, but harness {str(row[0])!r} is stored for this "
        "agent; the restart would be applied by that runtime"
    )
