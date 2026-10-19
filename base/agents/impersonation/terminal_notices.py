"""Ended-lease notice text and its single best-effort delivery to a Codex thread.

The host submits the notice once, when it observes the lease already ended. Host
acceptance is the only success this transport proves. Nothing is recorded and
nothing retries: a notice that is not accepted is reported by the caller and lost.
"""

from collections.abc import Callable
from typing import Any

from base.agents.impersonation import host_transport
from base.agents.impersonation.host_transport import AntigravityServer
from base.db import Database

# meiva:note
# The failure reason names the record path it looked for, so the event field must
# fit one path plus that sentence.
_REASON_MAX_CHARS = 400


def notice_text(snapshot: dict[str, Any]) -> str:
    """Describe exactly the ended historical lease, never a replacement's authority."""
    native = (
        "The agent was terminated; no active native runtime is implied. "
        if snapshot["reason"] == "terminated: agent was terminated"
        else "Native authority is restored; its durable handoff may still be restoring. "
    )
    return (
        f"Meiva impersonation lease {snapshot['session_id']} ({snapshot['lease_id']}) "
        f"for agent {snapshot['agent_id']} ended: {snapshot['status']}. "
        f"Reason: {snapshot['reason'] or snapshot['status']}. Ended at: {snapshot['ended_at']}. "
        "That lease no longer grants external control. " + native + "This notice concerns only "
        "the named lease and does not end or cancel any newer takeover or its work. "
        f"Notice ID: impersonation-ended:{snapshot['lease_id']}."
    )


# meiva:note
# The destination is the host conversation recorded on the lease, reached from
# outside the external session's environment: an Antigravity endpoint therefore
# comes from the record its request wrote on this host, never from a process scan.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
def send_ended_notice(
    db: Database,
    lease_id: str,
    *,
    resolve_antigravity: Callable[..., AntigravityServer] = (
        host_transport.resolve_antigravity_endpoint
    ),
) -> str | None:
    """Submit the ended lease's notice to its recorded Codex or Antigravity thread, once.

    Returns None when the notice was accepted or the lease has no host IPC
    destination (controller-session transports print their own notices), else a
    short failure reason. The submission has its own five-second deadline.
    """
    with db.connect() as conn:
        row = conn.execute(
            "SELECT terminal_notice_snapshot FROM agent_impersonations WHERE id=%s", (lease_id,)
        ).fetchone()
    snapshot = None if row is None else row[0]
    if not snapshot:
        return None
    if snapshot["provider"] == "antigravity":
        thread_id = snapshot["thread_id"]
        if not thread_id:
            return "the recorded Antigravity destination is not deliverable"
        try:
            server = resolve_antigravity(thread_id)
        except (OSError, RuntimeError, ValueError) as exc:
            return str(exc)[:_REASON_MAX_CHARS]
        return host_transport.antigravity_live_submit(
            thread_id, notice_text(snapshot), server=server
        )
    if snapshot["provider"] != "codex":
        return None
    endpoint, thread_id = snapshot["endpoint"], snapshot["thread_id"]
    if (
        not endpoint
        or not thread_id
        or endpoint == "unix://"
        or not endpoint.startswith(("unix://", "ws://", "wss://"))
    ):
        return "the recorded Codex destination is not deliverable"
    return host_transport.live_submit(thread_id, notice_text(snapshot), endpoint=endpoint)


# meiva:note
# One attempt, no retry and no ledger: the external conversation learns its lease
# ended here or not at all. A failure is an event rather than a return value
# because every caller is a host loop with no one to return to.
# meiva:link docs/conventions/agents/agent-impersonation-hosts.md
def deliver_ended_notice(db: Database, agent_id: int, lease_id: str) -> None:
    from base.log import logger

    error = send_ended_notice(db, lease_id)
    if error is None:
        return
    logger.error(
        "ended impersonation lease notice was not delivered and will not be retried",
        event="impersonation_terminal_notice_failed",
        agent_id=agent_id,
        lease_id=str(lease_id),
        error=error,
    )
