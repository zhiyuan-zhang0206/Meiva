# meiva:note
# The IM bridge daemon's run plan, decided from values: which adapters it loads from the
# operator's disabled list, and the exit status it leaves with.
#
# The daemon root imports and constructs exactly the adapters the plan names, in order, so a
# disabled adapter's module (and its SDK) is never imported; it leaves through the bounded hard
# exit with the code `exit_code` answers.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ADAPTER_NAMES", "AdapterPlan", "exit_code", "plan_adapters"]

# meiva:note
# The adapters the bridge ships, in load order.
ADAPTER_NAMES = ("telegram", "weixin", "feishu")


# meiva:note
# The adapters to load and the ones the configuration switched off, both in load order.
@dataclass(frozen=True)
class AdapterPlan:
    load: tuple[str, ...]
    disabled: tuple[str, ...]


# meiva:note
# Names in `disabled` that no adapter carries are ignored, as the operator's list may name an
# adapter a release no longer ships.
def plan_adapters(disabled: frozenset[str]) -> AdapterPlan:
    return AdapterPlan(
        load=tuple(name for name in ADAPTER_NAMES if name not in disabled),
        disabled=tuple(name for name in ADAPTER_NAMES if name in disabled),
    )


# meiva:note
# A crash that escaped the run is always a failure. An interrupt (the root's stop signal) is a
# normal stop unless the drain it triggered reported failures. A run that returned on its own
# stopped cleanly.
def exit_code(*, crashed: bool, interrupted: bool, drain_failures: int) -> int:
    if crashed:
        return 1
    if interrupted and drain_failures > 0:
        return 1
    return 0
