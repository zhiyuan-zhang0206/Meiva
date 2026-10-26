# meiva:note
# Pure framework lifecycle exit signals.

from __future__ import annotations

from base.host.exit_codes import IDLE_EXIT_CODE, SYSTEM_HALT_EXIT_CODE

__all__ = [
    "AgentRestart",
    "AgentTermination",
    "LifecycleExit",
    "SystemHalt",
]


# meiva:note
# Base for framework lifecycle signals — inherits SystemExit
# (not Exception), so broad try/except Exception won't accidentally
# swallow lifecycle transitions.
class LifecycleExit(SystemExit):
    pass


# meiva:note
# Raised on the success path of `terminate()`. Your process exits and
# has to be woken up by sending it a chat message (auto-resurrect).
class AgentTermination(LifecycleExit):
    def __init__(self) -> None:
        super().__init__(IDLE_EXIT_CODE)


# meiva:note
# Raised on the success path of `restart()` and `update()`. Your
# process exits and a fresh one comes up under the same agent id.
class AgentRestart(LifecycleExit):
    def __init__(self) -> None:
        super().__init__(IDLE_EXIT_CODE)


# meiva:note
# Raised by `compact()` — framework takes over history compaction. Agent doesn't raise directly.
class SystemHalt(LifecycleExit):
    def __init__(self) -> None:
        super().__init__(SYSTEM_HALT_EXIT_CODE)
