# meiva:note
# The relay's delivery methods, one per kind of host destination.
#
# A destination planned from the CLI options (`relay.destination`) binds once, at relay
# startup, to the thing a push is sent into, and every push reuses it: a per-message lookup
# is a second chance to fail after the first heartbeat promised delivery, and the relay's
# projected environment cannot discover an Antigravity CLI at all. Codex requires live Steer
# delivery: a refusal or a transport failure raises, with no queue fallback. Controller
# sessions (Claude, dsh) deliver the relay's terminal notices themselves; the Codex and
# Antigravity native hosts submit theirs once when they see the lease end.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO, assert_never
from uuid import UUID

from base.agents.impersonation.host_transport import (
    AntigravityServer,
    antigravity_live_submit,
    default_control_endpoint,
    default_control_socket,
    live_submit,
    read_antigravity_endpoint_file,
    resolve_antigravity_endpoint,
)
from cli.commands.agents.relay.destination import (
    AntigravityConversation,
    ClaudeSession,
    CodexThread,
    DshSession,
    RelayDestination,
    resolve_codex_endpoint,
)

__all__ = [
    "AntigravitySend",
    "ClaudeStdout",
    "CodexSteer",
    "DshStdout",
    "RelayAdapter",
    "bind_adapter",
    "codex_endpoint",
    "deliver",
    "notifies_terminal",
]


# meiva:note
# Flush text to the Claude Monitor that owns this relay's stdout, which the invocation names.
@dataclass(frozen=True)
class ClaudeStdout:
    stream: TextIO


# meiva:note
# Keep each envelope in one JSON line of the owning dsh plugin's stdout, which the invocation
# names.
@dataclass(frozen=True)
class DshStdout:
    stream: TextIO


# meiva:note
# Steer an existing Codex thread through the app server at `endpoint`.
@dataclass(frozen=True)
class CodexSteer:
    thread_id: UUID
    endpoint: str


# meiva:note
# SendAgentMessage into an existing Antigravity conversation through the one language
# server this relay resolved at startup.
@dataclass(frozen=True)
class AntigravitySend:
    thread_id: UUID
    server: AntigravityServer


RelayAdapter = ClaudeStdout | DshStdout | CodexSteer | AntigravitySend


# meiva:note
# The Antigravity language server this relay may use. A relay spawned by the accepting
# runtime is handed the record the request wrote, by path; a hand-started relay falls back to
# the same lookup (its own host-provided environment, then that record). Neither path guesses
# at a language_server process, which a standalone `agy` CLI never produces.
def _antigravity_server(thread_id: UUID, endpoint_record: str | None) -> AntigravityServer:
    if endpoint_record is None:
        return resolve_antigravity_endpoint(str(thread_id))
    path = Path(endpoint_record)
    server = read_antigravity_endpoint_file(path)
    if server is None:
        raise RuntimeError(f"the request's Antigravity endpoint record is missing at {path}")
    return server


# meiva:note
# The Codex app-server endpoint for `remote`, with the local daemon's socket resolved the way
# `codex` resolves it from CODEX_HOME. `codex_home` is that variable as the invocation saw it
# (None when unset), so the lookup never reads the process environment itself.
def codex_endpoint(remote: str | None, *, codex_home: str | None) -> str:
    env = {} if codex_home is None else {"CODEX_HOME": codex_home}
    return resolve_codex_endpoint(
        remote,
        default_endpoint=default_control_endpoint(env=env),
        default_socket=str(default_control_socket(env=env)),
    )


# meiva:note
# Bind the planned destination before the relay opens its inbox, so an unreachable host fails
# startup and rolls the acceptance back instead of failing after the first heartbeat.
def bind_adapter(
    destination: RelayDestination, *, codex_home: str | None, stdout: TextIO
) -> RelayAdapter:
    match destination:
        case ClaudeSession():
            return ClaudeStdout(stdout)
        case DshSession():
            return DshStdout(stdout)
        case CodexThread(thread_id=thread_id, remote=remote):
            return CodexSteer(thread_id, codex_endpoint(remote, codex_home=codex_home))
        case AntigravityConversation(thread_id=thread_id, endpoint_record=record):
            return AntigravitySend(thread_id, _antigravity_server(thread_id, record))
        case _:
            assert_never(destination)


# meiva:note
# Whether the relay words the lease's end itself. Native hosts submit their own ended notice
# once, when they observe it, so a second one from the relay would duplicate it.
def notifies_terminal(adapter: RelayAdapter) -> bool:
    match adapter:
        case ClaudeStdout() | DshStdout():
            return True
        case CodexSteer() | AntigravitySend():
            return False
        case _:
            assert_never(adapter)


# meiva:note
# Push one message into the host; raises when the host did not accept it.
def deliver(adapter: RelayAdapter, message: str) -> None:
    match adapter:
        case ClaudeStdout(stream=stream):
            print(message, file=stream, flush=True)
        case DshStdout(stream=stream):
            print(json.dumps(message, ensure_ascii=False), file=stream, flush=True)
        case CodexSteer(thread_id=thread_id, endpoint=endpoint):
            reason = live_submit(str(thread_id), message, endpoint=endpoint)
            if reason is not None:
                raise RuntimeError(f"Codex Steer delivery failed: {reason}")
        case AntigravitySend(thread_id=thread_id, server=server):
            reason = antigravity_live_submit(str(thread_id), message, server=server)
            if reason is not None:
                raise RuntimeError(f"Antigravity SendAgentMessage delivery failed: {reason}")
        case _:
            assert_never(adapter)
