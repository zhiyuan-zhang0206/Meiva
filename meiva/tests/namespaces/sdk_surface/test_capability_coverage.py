# meiva:note
# Anti-drift guard for `meiva/impersonation/capabilities.py`: the map may not fall
# behind the surface. `instrument_targets` is the same walk the metering recorder
# installs over, so "classified" here means "the gate can see it". Coverage is
# asserted for what this process exposes; a bare test process has no `tasks` /
# `ui.notify`, and the Fleet plugin's own tests check those against its
# declarations. A rename therefore always lands as a new, unclassified entry first.

from __future__ import annotations

from base.agents.impersonation.capabilities import Capability
from meiva.impersonation import capabilities
from meiva.sdk_surface import metering


def _surface() -> set[str]:
    return {fq for _parent, _attr, fq in metering.instrument_targets()}


def test_every_agent_visible_entry_has_a_capability() -> None:
    fqs = sorted(_surface())
    assert len(fqs) > 40, (
        f"the surface walk found only {len(fqs)} entries: it is looking in the wrong place"
    )
    unclassified = [fq for fq in fqs if capabilities.capability_for(fq) is None]
    assert unclassified == [], (
        "these agent-visible SDK entries have no declared capability; classify each one in "
        f"meiva/sdk_surface/capabilities.py: {unclassified}"
    )


def test_dynamic_mcp_tools_need_machine_authority() -> None:
    assert capabilities.capability_for("mcps.chrome.navigate") is Capability.SHELL
    assert capabilities.capability_for("mcps.notion.API-post-page") is Capability.SHELL


def test_an_unclassified_entry_has_no_capability() -> None:
    assert capabilities.capability_for("no.such.entry") is None


# meiva:note
# A capability nothing requires is dead vocabulary; a rename would hide there.
def test_every_capability_in_the_vocabulary_is_reachable() -> None:
    used = set(capabilities.ENTRY_CAPABILITIES.values()) | {
        capability for _prefix, capability in capabilities.PREFIX_CAPABILITIES
    }
    assert used == set(Capability), f"unused capabilities: {set(Capability) - used}"
