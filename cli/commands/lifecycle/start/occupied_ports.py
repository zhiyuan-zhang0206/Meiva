# meiva:note
# What a start says when another unit already answers on a health port it was about to bind.
#
# A daemon launched onto a taken port dies on 'address already in use'; one launched onto a
# relayed port is worse, because the watchdog's probe is answered by the other home's daemon and
# the failure reads as green. The start therefore refuses before anything launches and names the
# occupants and the stopgap. Optional capabilities cannot block a start: only the services
# core to serving are probed.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Occupant", "refusal_text"]


# meiva:note
# One health port another unit answers on: the roster session and the probe's own words.
@dataclass(frozen=True)
class Occupant:
    session: str
    name: str
    detail: str


# meiva:note
# The stderr report of a start refused over `occupants`.
#
# The probe's detail already names the occupant's home and URL, the two facts an operator
# needs to decide which unit should move.
def refusal_text(occupants: tuple[Occupant, ...]) -> str:
    listed = "\n".join(f"    {occupant.name}: {occupant.detail}" for occupant in occupants)
    drop = " ".join(f"--disable-service {occupant.session}" for occupant in occupants)
    return (
        "\n✗ another unit already answers on this unit's daemon health ports:\n"
        f"{listed}\n"
        "\n  Not starting — NOTHING was launched, including the gateway and the frontend. "
        "Launching onto a held port dies on 'address already in use'; launching onto a "
        "RELAYED one is worse, because the watchdog's probe is answered by the other home's "
        "daemon and the failure reads as green.\n"
        "  Stop the listed daemon, then retry `meiva start`. To bring the rest of this unit "
        f"up meanwhile, drop the listed daemons from this start: {drop}\n"
        "  (that daemon then does not run at all — it is a stopgap, not the fix)."
    )
