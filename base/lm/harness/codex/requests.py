from __future__ import annotations

from dataclasses import dataclass

from base.lm.harness.codex.frames import JsonObject, RequestParams, as_object, as_text
from base.lm.harness.codex.mapping import approval_denial

# meiva:note
# What this bridge tells the app-server about itself. The version is the protocol handshake's, not a
# package version: the app-server only needs to see that the client names itself.
CLIENT_NAME = "meiva-codex-bridge"
CLIENT_VERSION = "1"

# meiva:note
# The notification that ends the handshake. `initialize` answers; until `initialized` arrives the
# server treats every other call as premature.
INITIALIZED = "initialized"

# meiva:note
# The methods that ask a human to allow one action: the two current approval requests and the
# legacy patch one. A decline of any of them is recorded on the turn as a permission denial.
_APPROVAL_METHODS = frozenset(
    {
        "item/commandExecution/requestApproval",
        "item/fileChange/requestApproval",
        "applyPatchApproval",
    }
)


def initialize_request() -> tuple[str, JsonObject]:
    return (
        "initialize",
        {
            "clientInfo": {"name": CLIENT_NAME, "version": CLIENT_VERSION},
            "capabilities": {},
        },
    )


# meiva:note
# The method and payload that open the thread: a recorded thread is resumed by id; a fresh one starts
# with the sandbox, the approval policy and the model the caller chose, and inherits the whole shell
# environment so a command the turn runs sees what the bridge sees. `model` is omitted rather than sent empty: an unset model means the
# account's own default, not a request for no model.
def open_thread_request(
    resume: str | None,
    *,
    cwd: str,
    model: str | None,
    approval_policy: str,
    sandbox: str,
) -> tuple[str, JsonObject]:
    if resume:
        return ("thread/resume", {"threadId": resume})
    params: JsonObject = {
        "cwd": cwd,
        "approvalPolicy": approval_policy,
        "sandbox": sandbox,
        "config": {"shell_environment_policy": {"inherit": "all"}},
    }
    if model:
        params["model"] = model
    return ("thread/start", params)


def turn_start_request(thread_id: str | None, text: str) -> tuple[str, JsonObject]:
    return ("turn/start", {"threadId": thread_id, "input": [{"type": "text", "text": text}]})


def interrupt_request(thread_id: str, turn_id: str) -> tuple[str, JsonObject]:
    return ("turn/interrupt", {"threadId": thread_id, "turnId": turn_id})


# meiva:note
# The frame one server request is answered with, and the denial to record when it is one this
# bridge refuses: a request the bridge declines is an action the turn did not take, and the agent
# reads the refusal from the denial the turn carries.
@dataclass(frozen=True, slots=True)
class RequestAnswer:
    answer: JsonObject
    denial: str | None = None


# meiva:note
# The bridge owns no approval surface of its own: it answers with the decision the caller configured
# and, for the two current approval methods, that decision is the answer verbatim. `applyPatchApproval`
# keeps the legacy vocabulary (`approved`/`denied`) because the method predates `accept`/`decline`.
# Permissions are granted empty and user input is answered empty: inventing either would let an agent
# believe it had widened its sandbox or heard from a person.
def answer_request(method: str, params: RequestParams, *, decision: str) -> RequestAnswer:
    if method in _APPROVAL_METHODS:
        denial = approval_denial(method, params) if decision == "decline" else None
        if method == "applyPatchApproval":
            return RequestAnswer(
                {"decision": "approved" if decision == "accept" else "denied"}, denial
            )
        return RequestAnswer({"decision": decision}, denial)
    if method == "item/permissions/requestApproval":
        return RequestAnswer({"permissions": {}, "scope": "turn"})
    if method == "item/tool/requestUserInput":
        return RequestAnswer({"answers": {}})
    return RequestAnswer({})


# meiva:note
# The thread an answered `thread/start` or `thread/resume` opened: its id, and the model and working
# directory the app-server reports for it. A reply that names no id keeps the id that was resumed, and
# a fresh thread that names none stays empty rather than borrowing the request's.
@dataclass(frozen=True, slots=True)
class OpenedThread:
    session_id: str
    model: str | None
    cwd: str

    @classmethod
    def of(
        cls, result: RequestParams, *, resume: str | None, model: str | None, cwd: str
    ) -> OpenedThread:
        thread = as_object(result.get("thread"))
        return cls(
            session_id=str(thread.get("id") or (resume or "")),
            model=as_text(result.get("model")) or as_text(thread.get("model")) or model,
            cwd=as_text(result.get("cwd")) or as_text(thread.get("cwd")) or cwd,
        )
