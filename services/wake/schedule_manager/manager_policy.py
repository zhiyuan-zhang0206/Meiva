# meiva:note
# The schedule manager's tunables, read once by its composition root.
#
# The daemon builds this from the gateway configuration when it starts; a test builds the one it
# wants. The manager never reads settings or the environment itself.

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["ManagerPolicy"]


# meiva:note
# Limits and facts the manager decides with.
#
# ``auto_restart_max_per_window`` automatic relaunches are allowed per schedule inside one
# rolling ``auto_restart_window_seconds`` after its session host was replaced.
# ``recovery_gate_alert_seconds`` is how long the serving-receipt gate may refuse relaunches
# before the manager reports it. ``timezone`` is the cluster's: the runner's cron math must
# fire on it, not on whatever a session's ambient environment carries.
@dataclass(frozen=True)
class ManagerPolicy:
    auto_restart_max_per_window: int
    auto_restart_window_seconds: float
    recovery_gate_alert_seconds: float
    timezone: str

    def __post_init__(self) -> None:
        if not (
            self.auto_restart_max_per_window > 0
            and self.auto_restart_window_seconds > 0
            and self.recovery_gate_alert_seconds > 0
        ):
            raise ValueError("every schedule manager limit must be positive")
