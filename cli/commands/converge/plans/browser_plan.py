# meiva:note
# What the browser capability step does on this host.
#
# A pure decision over the host's browser incapability reason (None when it can run a headed
# Chrome), whether the browser is enabled and whether a human is at the terminal. The shell
# prints the notice or warning, or offers the profile seed.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "BrowserDisabled",
    "BrowserIncapable",
    "BrowserNotApplicable",
    "BrowserPlan",
    "OfferProfileSeed",
    "plan_browser",
]


# meiva:note
# The browser is switched off: nothing to probe.
@dataclass(frozen=True)
class BrowserDisabled: ...


# meiva:note
# A display-less host: an informational notice, never a warning.
@dataclass(frozen=True)
class BrowserNotApplicable:
    reason: str


# meiva:note
# A repairable gap (no Chrome, no npx): a prominent warning that does not fail converge.
@dataclass(frozen=True)
class BrowserIncapable:
    reason: str


# meiva:note
# A capable host: offer, once, to seed the dedicated Chrome profile.
#
# The offer is a security-gated choice that only fires when a human is at the terminal;
# watchdog respawns and rollout-driven starts take the fresh default and never block.
@dataclass(frozen=True)
class OfferProfileSeed:
    interactive: bool


BrowserPlan = BrowserDisabled | BrowserNotApplicable | BrowserIncapable | OfferProfileSeed


def plan_browser(*, enabled: bool, incapability: str | None, interactive: bool) -> BrowserPlan:
    if not enabled:
        return BrowserDisabled()
    if incapability is None:
        return OfferProfileSeed(interactive)
    if incapability.startswith("no display"):
        return BrowserNotApplicable(incapability)
    return BrowserIncapable(incapability)
