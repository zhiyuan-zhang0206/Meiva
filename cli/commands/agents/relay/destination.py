# meiva:note
# Where one relay delivers, as values: the closed set of destinations and the one rule that
# decides which combination of `--provider`, `--thread-id` and `--codex-remote` names one.
#
# The CLI boundary asks `relay_spec_problem` for a usage error before any command code runs,
# and the relay's own startup plans its destination through the same rule, so the two cannot
# disagree. The shared validator in `base.agents.impersonation` stays in place as the
# server-side defense.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

__all__ = [
    "AntigravityConversation",
    "ClaudeSession",
    "CodexThread",
    "DshSession",
    "RelayDestination",
    "plan_destination",
    "relay_spec_problem",
    "resolve_codex_endpoint",
]


# meiva:note
# Text goes to the Claude Monitor that owns the relay's stdout, one flushed block per push.
@dataclass(frozen=True)
class ClaudeSession:
    pass


# meiva:note
# Text goes to the dsh plugin that owns the relay's stdout, one JSON string per line.
@dataclass(frozen=True)
class DshSession:
    pass


# meiva:note
# A live Steer into an existing Codex thread. `remote` is the app-server endpoint the
# session recorded; None means the local daemon's default control socket.
@dataclass(frozen=True)
class CodexThread:
    thread_id: UUID
    remote: str | None


# meiva:note
# A SendAgentMessage into an existing Antigravity conversation. `endpoint_record` is the path
# of the record the request wrote on this host; None means the relay resolves the endpoint
# from its own host-provided environment, then the request's record by conversation.
@dataclass(frozen=True)
class AntigravityConversation:
    thread_id: UUID
    endpoint_record: str | None


RelayDestination = ClaudeSession | DshSession | CodexThread | AntigravityConversation


# meiva:note
# Why this combination of relay options names no destination, or None when it does.
#
# `env_thread_id` is the Antigravity conversation id the host injected into the commands it
# runs: a request may take its thread from it, a relay never does and passes None.
def relay_spec_problem(
    provider: str,
    thread_id: str | None,
    codex_remote: str | None,
    *,
    env_thread_id: str | None,
) -> str | None:
    if provider == "codex":
        if not thread_id:
            return (
                "the codex relay needs --thread-id — the existing Codex session UUID "
                "it delivers into (optionally with a --codex-remote unix:// or ws:// endpoint)"
            )
        if codex_remote is not None and not codex_remote.startswith(("unix://", "ws://")):
            return "codex remote must be a unix:// or ws:// endpoint"
    elif provider == "antigravity":
        if codex_remote is not None:
            return "the antigravity relay does not use --codex-remote"
        if not thread_id and not env_thread_id:
            return (
                "the antigravity relay needs --thread-id (or ANTIGRAVITY_CONVERSATION_ID "
                "in the environment) — the existing Antigravity conversation UUID it "
                "delivers into"
            )
    elif provider in ("claude", "dsh"):
        if thread_id is not None or codex_remote is not None:
            return f"the {provider} relay routes to its owner; drop --thread-id/--codex-remote"
    else:
        return f"unknown relay provider {provider!r}"
    return None


# meiva:note
# The destination a relay started with these options delivers to; ValueError when they name
# none. The thread id is parsed here, so a malformed UUID fails before any lease work.
def plan_destination(
    provider: str,
    thread_id: str | None,
    *,
    codex_remote: str | None,
    endpoint_record: str | None,
) -> RelayDestination:
    problem = relay_spec_problem(provider, thread_id, codex_remote, env_thread_id=None)
    if problem is not None:
        raise ValueError(problem)
    match provider:
        case "codex":
            return CodexThread(UUID(_required(thread_id)), codex_remote)
        case "antigravity":
            return AntigravityConversation(UUID(_required(thread_id)), endpoint_record)
        case "claude":
            return ClaudeSession()
        case "dsh":
            return DshSession()
        case _:
            raise ValueError(f"unknown relay provider {provider!r}")


def _required(thread_id: str | None) -> str:
    if thread_id is None:
        raise ValueError("the relay needs --thread-id")
    return thread_id


# meiva:note
# The app-server endpoint a Codex relay or request steers: the one the session recorded, else
# the local daemon's control socket once it exists. A bare `unix://` names that socket too.
# Delivery by a queued Pending message is not supported, so with neither an endpoint nor a
# daemon the relay refuses instead of guessing.
def resolve_codex_endpoint(
    remote: str | None, *, default_endpoint: str | None, default_socket: str
) -> str:
    endpoint = remote or default_endpoint
    if endpoint is None:
        raise RuntimeError(
            "Codex Steer delivery requires the existing session's app server; "
            "pass --codex-remote with the same endpoint used by codex --remote. "
            "Pending delivery via codex queue is not supported."
        )
    return f"unix://{default_socket}" if endpoint == "unix://" else endpoint
