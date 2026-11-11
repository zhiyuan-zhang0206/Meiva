from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from base.host.boundary.json_values import is_object_dict, is_object_list


# meiva:note
# What `GetCascadeTrajectory` says about a conversation, decoded and checked. The method is not a
# public interface (agy 1.5.5): its answer is `{trajectory: {steps: [...]}, status, numTotalSteps}` with
# `status` one of `CASCADE_RUN_STATUS_RUNNING` / `CASCADE_RUN_STATUS_IDLE`. It is the only live signal of
# whether a turn runs, including a turn whose events never reach stdout. A reply of any other shape,
# or a status this adapter does not know, is an `AgyInterfaceError` and never read as "idle": treating
# an unreadable answer as idle is how an input would be sent into a turn nobody can see.
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


@dataclass(frozen=True, slots=True)
class TrajectoryStep:
    index: int
    kind: str
    client_message_id: str | None


@dataclass(frozen=True, slots=True)
class Trajectory:
    status: RunStatus
    num_steps: int
    steps: tuple[TrajectoryStep, ...]

    def client_message_ids(self) -> tuple[str, ...]:
        return tuple(step.client_message_id for step in self.steps if step.client_message_id)


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
    user_input = entry.get("userInput")
    client_id = user_input.get("clientMessageId") if is_object_dict(user_input) else None
    return TrajectoryStep(
        index=index,
        kind=kind if isinstance(kind, str) else "",
        client_message_id=client_id if isinstance(client_id, str) and client_id else None,
    )


# meiva:note
# Where an input goes, decided before it is sent. A running conversation takes it through
# `SendUserCascadeMessage`. An idle one must not: agy answers 200 and runs a turn nobody can see on
# stdout (a phantom turn), so an input for an idle conversation starts a turn on stdin, and when our
# own turn's result has not been read yet it waits behind that turn instead.
class Route(StrEnum):
    STEER = "steer"
    START = "start"
    QUEUE = "queue"


def route(status: RunStatus | None, *, ticket_active: bool) -> Route:
    if status is RunStatus.RUNNING:
        return Route.STEER
    return Route.QUEUE if ticket_active else Route.START


# meiva:note
# What the poll of a conversation with no turn of ours running means. Running with no ticket and no
# phantom open is a turn we can only see here: open one. Idle with a phantom open is its end.
@dataclass(frozen=True, slots=True)
class PhantomTurn:
    turn_id: str
    first_step: int


class PhantomAction(StrEnum):
    NONE = "none"
    OPEN = "open"
    CLOSE = "close"


def phantom_action(status: RunStatus, *, ticket_active: bool, phantom_open: bool) -> PhantomAction:
    if ticket_active:
        return PhantomAction.NONE
    if status is RunStatus.RUNNING and not phantom_open:
        return PhantomAction.OPEN
    if status is RunStatus.IDLE and phantom_open:
        return PhantomAction.CLOSE
    return PhantomAction.NONE
