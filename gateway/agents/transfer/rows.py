# meiva:note
# The rows the transfer's precheck and cutover queries read, one frozen dataclass per query shape,
# a field per selected column named as the column.

from __future__ import annotations

from dataclasses import dataclass

__all__ = [
    "AgentMetaRow",
    "LifecycleRow",
    "MachineRow",
    "TargetGateRow",
    "UploadBatchRow",
]


# meiva:note
# `agents_meta` columns a transfer reads as facts about the agent.
@dataclass(frozen=True)
class AgentMetaRow:
    machine: str
    status: str
    harness: str


# meiva:note
# The queued restart or terminate command that blocks a transfer.
@dataclass(frozen=True)
class LifecycleRow:
    kind: str
    status: str


# meiva:note
# The upload batch still receiving or pending for the agent.
@dataclass(frozen=True)
class UploadBatchRow:
    batch_id: str
    state: str


# meiva:note
# A machine's registry row as the transfer gates read it.
@dataclass(frozen=True)
class MachineRow:
    gateway_url: str | None
    role: list[str] | None
    paused: bool
    stopped: bool


# meiva:note
# The target machine's gate under `FOR SHARE`: paused, stopped and whether it runs agents.
@dataclass(frozen=True)
class TargetGateRow:
    paused: bool
    stopped: bool
    runs_agents: bool
