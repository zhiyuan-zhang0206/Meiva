# meiva:note
# What `meiva impersonate request` decides before it asks for a lease, and what it tells the
# operator afterwards, as values.
#
# A request names the relay that will push into the host. This module settles which host
# the options name (and the host facts the request may take from the environment), the
# credential stub a dsh session consumes, and the hand-off instructions the request prints:
# the shell then records the endpoint, asks the lease service and writes the stub.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import PurePath
from typing import assert_never
from uuid import UUID

__all__ = ["Handoff", "RequestPlan", "handoff_hint", "plan_request", "relay_stub_text"]


# meiva:note
# How the relay starts once the lease exists. The claude relay runs inside the controller
# session; the dsh relay starts from the plugin that consumes the credential stub; the
# runtime starts the codex and antigravity relays itself at activation.
class Handoff(Enum):
    CLAUDE = "claude"
    DSH = "dsh"
    CODEX = "codex"
    ANTIGRAVITY = "antigravity"


# meiva:note
# The facts the shell needs to ask for the lease: which hand-off applies, the host thread
# (an Antigravity conversation is normalized to its canonical UUID), the Codex app-server
# endpoint as given (None means the daemon's default socket), and for dsh the stub path the
# plugin exports to the session's shell.
@dataclass(frozen=True)
class RequestPlan:
    handoff: Handoff
    thread_id: str | None
    codex_remote: str | None
    dsh_stub: PurePath | None


# meiva:note
# The plan for a request with these options and environment facts; ValueError when the host
# cannot be reached from them.
#
# The dsh request is checked before any lease exists: without the plugin nothing would start
# the relay. DeepSeek Harness uploads session logs with its model requests by default, so the
# credential goes into the plugin's private per-session stub instead of the printed response.
# The Antigravity CLI injects its conversation id only into the commands it runs, so a request
# run from one may omit `--thread-id`.
def plan_request(
    provider: str,
    thread_id: str | None,
    codex_remote: str | None,
    *,
    conversation_env: str | None,
    dsh_stub_env: str | None,
) -> RequestPlan:
    match provider:
        case "claude":
            return RequestPlan(Handoff.CLAUDE, thread_id, codex_remote, None)
        case "dsh":
            if not dsh_stub_env:
                raise ValueError(
                    "the dsh relay needs the Meiva relay plugin (meiva-relay.mjs) loaded into "
                    "this DeepSeek Harness session and the request run from that session's "
                    "shell tool (DSH_MEIVA_RELAY_STUB is unset; see the host conventions)"
                )
            return RequestPlan(Handoff.DSH, thread_id, codex_remote, PurePath(dsh_stub_env))
        case "codex":
            return RequestPlan(Handoff.CODEX, thread_id, codex_remote, None)
        case "antigravity":
            conversation = thread_id or (conversation_env or "").strip() or None
            if not conversation:
                raise ValueError(
                    "the antigravity relay needs --thread-id or ANTIGRAVITY_CONVERSATION_ID "
                    "in the environment"
                )
            return RequestPlan(Handoff.ANTIGRAVITY, str(UUID(conversation)), codex_remote, None)
        case _:
            raise ValueError(f"unknown relay provider {provider!r}")


# meiva:note
# The credential stub the dsh plugin consumes once: it deletes the file immediately after
# loading it into the relay's environment.
def relay_stub_text(*, agent_id: int, session_id: int, token: str, interpreter: str | None) -> str:
    text = f"SID={session_id}\nAGENT={agent_id}\nMEIVA_IMPERSONATION_RELAY_TOKEN={token}\n"
    if interpreter is not None:
        text += f"MEIVA_IMPERSONATION_RELAY_PY={interpreter}\n"
    return text


# meiva:note
# What the request tells the operator about starting the relay, for stderr.
def handoff_hint(plan: RequestPlan) -> str:
    match plan.handoff:
        case Handoff.DSH:
            return (
                "The Meiva relay plugin in this DeepSeek Harness session starts the dsh "
                f"relay automatically from its credential stub ({plan.dsh_stub}) and pushes "
                "each inbound batch into this session as steering input. If no relay "
                "heartbeat starts, preparation fails and the takeover is rejected."
            )
        case Handoff.ANTIGRAVITY:
            return (
                "The runtime starts the antigravity relay automatically at activation; "
                "no relay process starts here. The relay pushes each inbound batch into "
                "this conversation via SendAgentMessage. Wait for the start message "
                "before acting as the agent."
            )
        case Handoff.CODEX:
            return (
                "The runtime starts the codex relay automatically at activation; "
                "no relay process starts here. When your session runs an explicit app "
                "server, pass --codex-remote with its endpoint so the relay reaches the "
                "same server the session uses (Steer delivery, no Pending fallback; "
                "see the host conventions). A relay "
                "that cannot start rolls the takeover back loudly (status becomes "
                "rejected with the reason)."
            )
        case Handoff.CLAUDE:
            return (
                "Start the claude relay (meiva impersonate relay) inside the controller "
                "session immediately, with MEIVA_IMPERSONATION_RELAY_TOKEN set to the "
                "relay token printed above; arm it as a Monitor watch with timeout_ms "
                "1800000 and re-arm on each expiry notice (see the host conventions). "
                "Preparation fails without its heartbeat."
            )
        case _:
            assert_never(plan.handoff)
