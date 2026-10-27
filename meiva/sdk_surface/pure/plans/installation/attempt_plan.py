# meiva:note
# What the installation slot says about a plugin load, decided over the value the writer read from
# it: the failure a failed load left behind, and the marker that a load is in flight. The writers
# (`wraps.py` for the marker, `sdk_disable.py` for the failure) read the slot, call these functions
# and store what they return; nothing here touches the module.
# meiva:link meiva/docs/sdk-surface/install-lifetime.meiva.okf.md

from __future__ import annotations

from meiva.sdk_surface.installation import Installation, Loading
from meiva.sdk_surface.pure.plans.installation.slot_plan import Slot, SlotWrite


# meiva:note
# The failure a prior load left in the slot, to be propagated instead of answering as if the
# surface were ready; None when the slot holds no recorded failure.
def load_failure(slot: Slot) -> BaseException | None:
    if isinstance(slot, Loading):
        return slot.failure
    return None


# meiva:note
# The write that records a load as attempted: a `Loading` marker around the installation the slot
# holds, if any. None (no write) when the slot already holds a marker.
def attempt_marked(slot: Slot) -> SlotWrite | None:
    if isinstance(slot, Loading):
        return None
    return SlotWrite(Loading(slot if isinstance(slot, Installation) else None))


# meiva:note
# The write that clears the in-flight marker: the installation it wraps takes the slot back. None
# (no write) when the slot holds no marker.
def attempt_cleared(slot: Slot) -> SlotWrite | None:
    if isinstance(slot, Loading):
        return SlotWrite(slot.installation)
    return None
