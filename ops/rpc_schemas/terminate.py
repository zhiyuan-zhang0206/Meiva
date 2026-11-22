"""Terminate-exchange wire models — request, response, and the open-task hint.

The `ops.rpc_schemas` door re-exports these names.
"""

from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from base.agents.messages.envelope import validate_source
from base.api_contracts.agent_contract import ShellSessionKillTiming, TerminateResult
from base.api_contracts.caller_identity import CallerIdentity, is_caller_source
from ops.rpc_schemas.content import UserContent


# meiva:note
# POST /api/agents/{id}/terminate request body — fully optional.
#
# `force` defaults to False for graceful termination. True directly kills the
# detached process and force-updates status when the agent cannot reach claim.
#
# Every termination closes every shell session the agent owns on its home machine
# (watchers included), with no per-session notice. A graceful termination does so
# after its last step commits; force and already-terminated requests do so before
# the response. The legacy `kill_all_shell_sessions` field remains as an audit
# compatibility marker. A periodic reaper closes leftovers when a host is down
# during the immediate cleanup. A terminated agent is otherwise an ordinary terminated agent — any new
# message may resurrect it
# (docs/decisions/agents/lifecycle/2026-09-27-terminate-has-no-closed-state.md).
#
# `source` defaults to "user"; SDK paths pass f"agent:{my_id}". Claim
# includes this source in the lifecycle marker shown to the agent.
#
# `message`, when present, is queued as pending chat immediately before the
# terminate inbound (visible on resurrection without running another turn),
# carried on the termination marker/audit record, and forwarded in the
# `agent_terminated` system note sent to the agent's nearest living ancestor
# (skipped only when the caller itself is that exact nearest living ancestor).
class TerminateAgentRequest(BaseModel):
    force: bool = Field(default=False)
    kill_all_shell_sessions: bool = Field(default=False)
    source: str = Field(default="user", min_length=1, max_length=64)
    message: UserContent | None = None

    @field_validator("source")
    @classmethod
    def _check_source(cls, value: str) -> str:
        # Lifecycle audit reasons include opaque values such as machine-pause;
        # they are not chat envelope source identifiers, except a declared
        # external agent, which must be well-formed.
        if is_caller_source(value):
            CallerIdentity.from_source(value)
        return value

    @model_validator(mode="after")
    def _validate_message_source(self) -> "TerminateAgentRequest":
        if self.message is not None:
            validate_source(self.source)
        return self


class OpenTaskRow(BaseModel):
    """One open task a terminating agent still owns; `updated_at` is ISO-8601.

    The terminate hint shows at most five, most recently updated first."""

    id: int
    title: str
    status: Literal["in_progress"]
    updated_at: str


class OpenTasksHint(BaseModel):
    """The open tasks (in_progress) a terminating agent still owns.

    Advisory only — termination proceeds either way. `tasks` holds at most the
    five most recently updated rows; `more` counts the ones beyond those."""

    count: int
    tasks: list[OpenTaskRow]
    more: int


# meiva:note
# The response reports timing and confirmed IDs for mandatory cleanup without
# promising that a graceful terminal host has already completed its final step.
class ShellSessionsKill(BaseModel):
    when: ShellSessionKillTiming
    killed: list[int] = Field(default_factory=list[int])


# meiva:note
# The registration row an agent's home machine holds for it, reported by `terminate` as
# `attached`.
#
# A bridge session writes one row when it starts a provider runtime as this agent and retires it
# when the session closes, so a row still on disk names the process driving that session.
# `expires_at` is the instant after which the row is stale — null when it never expires — and the
# caller judges it against its own clock rather than this machine's.
#
# The read filters on nothing, expiry included: a row whose `expires_at` has passed is still
# reported with that same past instant, so a caller can tell "nobody is attached" (null) from
# "a session was attached and its claim has lapsed" (a past `expires_at`).
#
# Advisory only: termination proceeds whether or not a row is present, and nothing refuses a
# request because somebody is attached. A response that could not read one carries null.
class HarnessAttachment(BaseModel):
    agent_id: int
    controller_pid: int
    expires_at: str | None = None


# meiva:note
# Terminal cleanup may be absent from a response only while a mixed-version home
# runner still predates the lifecycle contract.
class TerminateAgentResponse(BaseModel):
    status: TerminateResult
    open_tasks: OpenTasksHint | None = None
    shell_sessions: ShellSessionsKill | None = None
    attached: HarnessAttachment | None = None
