"""Provider transports for the shared impersonation relay process.

Adapters select the host destination, encode its input and declare who sends
terminal notices. Inbox reads, reservations, ACK windows, batching and retries
remain in the common relay loop.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import cast
from uuid import UUID

from base.agents.impersonation.host_transport import (
    AntigravityServer,
    antigravity_live_submit,
    discover_antigravity_server,
    live_submit,
    require_control_endpoint,
)


def _no_destination() -> None:
    pass


# meiva:note
# A bound sender, its terminal-notice ownership and the readiness probe the relay
# runs before its first heartbeat. The native host submits a Codex lease's ended
# notice once, when it observes the end; controller-session transports send their
# own notices.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
@dataclass(frozen=True)
class RelayAdapter:
    send: Callable[[str], None]
    notify_terminal: bool
    ready: Callable[[], None] = _no_destination


def emit_claude(message: str) -> None:
    """Flush text to the owning Claude Monitor."""
    print(message, flush=True)


def emit_dsh(message: str) -> None:
    """Keep each envelope in one JSON stdout line for the owning dsh plugin."""
    print(json.dumps(message, ensure_ascii=False), flush=True)


# meiva:note
# Bind the provider's destination before the relay opens its inbox. Codex requires
# live Steer delivery: refusal or transport failure raises without queue fallback,
# and controller-session destinations reject Codex options. The Antigravity
# destination resolves once and every push reuses it: a per-message lookup is a
# second chance to fail after the first heartbeat promised delivery, and the
# relay's projected environment cannot discover an Antigravity CLI at all.
# ``discover`` is the endpoint lookup — the host's live environment by default,
# the request's recorded endpoint when the relay is spawned outside that
# environment; a test supplies its own instead of patching.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
def resolve_adapter(
    provider: str,
    thread_id: str | None,
    *,
    codex_remote: str | None = None,
    discover: Callable[[], object] = discover_antigravity_server,
) -> RelayAdapter:
    if provider == "codex":
        if thread_id is None:
            raise ValueError("codex relay requires --thread-id for an existing session")
        target = UUID(thread_id)
        endpoint = require_control_endpoint(codex_remote)

        def send(message: str) -> None:
            reason = live_submit(str(target), message, endpoint=endpoint)
            if reason is not None:
                raise RuntimeError(f"Codex Steer delivery failed: {reason}")

        return RelayAdapter(send, notify_terminal=False)
    if provider == "antigravity":
        if codex_remote is not None:
            raise ValueError("the antigravity relay does not use --codex-remote")
        if thread_id is None:
            raise ValueError("antigravity relay requires --thread-id for an existing conversation")
        target = UUID(thread_id)
        resolved: AntigravityServer | None = None

        # meiva:note
        # One destination for the whole relay: re-resolving per message is a second
        # chance to fail after the first heartbeat already promised delivery.
        def destination() -> AntigravityServer:
            nonlocal resolved
            if resolved is None:
                resolved = cast("AntigravityServer", discover())
            return resolved

        def send_antigravity(message: str) -> None:
            reason = antigravity_live_submit(str(target), message, server=destination())
            if reason is not None:
                raise RuntimeError(f"Antigravity SendAgentMessage delivery failed: {reason}")

        def require_language_server() -> None:
            destination()

        return RelayAdapter(send_antigravity, notify_terminal=False, ready=require_language_server)
    if provider in ("claude", "dsh"):
        if codex_remote is not None or thread_id is not None:
            raise ValueError(
                f"The {provider} relay routes to its owner; omit --thread-id/--codex-remote"
            )
        return RelayAdapter(emit_claude if provider == "claude" else emit_dsh, notify_terminal=True)
    raise ValueError(f"Unknown relay provider: {provider}")
