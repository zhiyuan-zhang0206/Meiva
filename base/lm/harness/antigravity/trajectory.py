from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from base.host.boundary.json_values import is_object_dict, is_object_list
from base.lm.harness.antigravity.timing import duration_ms, timing_key


# meiva:note
# What `GetCascadeTrajectory` says about a conversation, decoded and checked. The method is not a
# public interface (agy 1.5.5): its answer is `{trajectory: {steps: [...]}, status, numTotalSteps}` with
# `status` one of `CASCADE_RUN_STATUS_RUNNING` / `CASCADE_RUN_STATUS_IDLE`. Agy 1.5.9 can report top-level
# IDLE while a child step remains live, so both levels are preserved and IDLE is never treated as an
# outer-turn terminal signal. A reply of any other shape, or an unknown status, is an `AgyInterfaceError`.
# meiva:link base/lm/harness/antigravity/docs/antigravity.md
class AgyInterfaceError(RuntimeError):
    pass


class RunStatus(StrEnum):
    RUNNING = "running"
    IDLE = "idle"


_STATUS = {
    "CASCADE_RUN_STATUS_RUNNING": RunStatus.RUNNING,
    "CASCADE_RUN_STATUS_IDLE": RunStatus.IDLE,
}

_LIVE_STEP_STATUSES = frozenset(
    {
        "CORTEX_STEP_STATUS_RUNNING",
        "CORTEX_STEP_STATUS_GENERATING",
    }
)
_PLANNER_STEP_KIND = "CORTEX_STEP_TYPE_PLANNER_RESPONSE"


@dataclass(frozen=True, slots=True)
class TrajectoryStep:
    index: int
    kind: str
    status: str
    client_message_id: str | None
    timing_key: str | None = None
    thinking_ms: int | None = None
    invalid_thinking_duration: bool = False


@dataclass(frozen=True, slots=True)
class Trajectory:
    status: RunStatus
    num_steps: int
    steps: tuple[TrajectoryStep, ...]

    def client_message_ids(self) -> tuple[str, ...]:
        return tuple(step.client_message_id for step in self.steps if step.client_message_id)

    def has_live_steps(self) -> bool:
        return any(step.status in _LIVE_STEP_STATUSES for step in self.steps)

    def has_live_planner_steps(self) -> bool:
        return any(
            step.kind == _PLANNER_STEP_KIND and step.status in _LIVE_STEP_STATUSES
            for step in self.steps
        )

    def has_live_background_steps(self) -> bool:
        return any(
            step.kind != _PLANNER_STEP_KIND and step.status in _LIVE_STEP_STATUSES
            for step in self.steps
        )

    def is_idle_without_planner(self) -> bool:
        return self.status is RunStatus.IDLE and not self.has_live_planner_steps()

    def is_reactive_background_idle(self) -> bool:
        return self.is_idle_without_planner() and self.has_live_background_steps()


def parse_trajectory(payload: Mapping[str, object]) -> Trajectory:
    raw_status = payload.get("status")
    status = _STATUS.get(raw_status) if isinstance(raw_status, str) else None
    if status is None:
        raise AgyInterfaceError(f"GetCascadeTrajectory returned an unknown status {raw_status!r}")
    # meiva:note
    # The server's JSON omits a zero-valued field: a conversation that has no step yet answers with
    # neither `numTotalSteps` nor `trajectory.steps` (recorded on agy 1.5.5 right after `init`). An
    # absent count is therefore zero, and an absent step list is empty only when the count is zero.
    total = payload.get("numTotalSteps", 0)
    if isinstance(total, bool) or not isinstance(total, int):
        raise AgyInterfaceError("GetCascadeTrajectory returned a non-integer numTotalSteps")
    trajectory = payload.get("trajectory")
    raw_steps = (
        trajectory.get("steps", [] if total == 0 else None) if is_object_dict(trajectory) else None
    )
    if not is_object_list(raw_steps):
        raise AgyInterfaceError("GetCascadeTrajectory returned no trajectory.steps list")
    steps = tuple(_step(index, entry) for index, entry in enumerate(_listed(raw_steps)))
    return Trajectory(status=status, num_steps=total, steps=steps)


def _listed(value: object) -> list[object]:
    return value if is_object_list(value) else []


def _step(index: int, entry: object) -> TrajectoryStep:
    if not is_object_dict(entry):
        raise AgyInterfaceError(f"trajectory step {index} is not an object")
    kind = entry.get("type")
    status = entry.get("status")
    user_input = entry.get("userInput")
    client_id = user_input.get("clientMessageId") if is_object_dict(user_input) else None
    planner = entry.get("plannerResponse")
    metadata = entry.get("metadata")
    terminal = status == "CORTEX_STEP_STATUS_DONE" and kind == "CORTEX_STEP_TYPE_PLANNER_RESPONSE"
    measured = planner if terminal and is_object_dict(planner) else {}
    meta = metadata if is_object_dict(metadata) else {}
    return TrajectoryStep(
        index=index,
        kind=kind if isinstance(kind, str) else "",
        status=status if isinstance(status, str) else "",
        client_message_id=client_id if isinstance(client_id, str) and client_id else None,
        timing_key=timing_key(index, meta.get("createdAt"), measured.get("thinking")),
        thinking_ms=duration_ms(measured.get("thinkingDuration")),
        invalid_thinking_duration=(
            measured.get("thinkingDuration") is not None
            and duration_ms(measured.get("thinkingDuration")) is None
        ),
    )
