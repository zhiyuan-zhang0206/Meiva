# meiva:note
# The transition a deferred restart command carries in its payload, as a value and the reader that
# checks it. The writer is `ops/lifecycle/deferred_restart.py`; the reader is the host's apply
# (`agent.ownership.hosted.apply_hosted_lifecycle`) and its pure plan.

from collections.abc import Mapping
from dataclasses import dataclass

from base.host.boundary.json_values import is_object_dict

# meiva:note
# The payload key of a restart command whose change waits for the turn boundary: the harness the
# agent runs on afterwards and the overlay merged into its settings when the restart is applied.
RESTART_TRANSITION = "restart_transition"


# meiva:note
# What a deferred restart (`meiva.self.restart` on an external harness, or a switch of harness from
# any harness) asks the host to do when it applies the claimed command: merge `config_overlay`
# (the `harness` key already removed) and, when `target_harness` differs from the stored harness,
# store it. The writer validated both before the row existed, so the apply is mechanical.
@dataclass(frozen=True, slots=True)
class RestartTransition:
    target_harness: str
    config_overlay: Mapping[str, object]

    def payload(self) -> dict[str, object]:
        return {
            RESTART_TRANSITION: {
                "target_harness": self.target_harness,
                "config_overlay": dict(self.config_overlay),
            }
        }


# meiva:note
# The transition a restart command carries; None for a command without one (the native restart,
# whose overlay was merged when it was queued). A payload that names a transition of another shape
# raises: applying half of a harness switch is worse than refusing it.
def restart_transition_of(payload: object) -> RestartTransition | None:
    if not is_object_dict(payload) or RESTART_TRANSITION not in payload:
        return None
    raw = payload[RESTART_TRANSITION]
    if not is_object_dict(raw):
        raise TypeError(f"{RESTART_TRANSITION} must be an object, got {type(raw).__name__}")
    target = raw.get("target_harness")
    overlay = raw.get("config_overlay")
    if not isinstance(target, str) or not target:
        raise TypeError(f"{RESTART_TRANSITION}.target_harness must be a non-empty string")
    if not is_object_dict(overlay) or "harness" in overlay:
        raise TypeError(
            f"{RESTART_TRANSITION}.config_overlay must be an object without a harness key"
        )
    return RestartTransition(target, overlay)
