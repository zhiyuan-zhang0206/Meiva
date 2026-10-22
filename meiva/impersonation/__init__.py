"""Attach an external controller to an active Meiva takeover session."""

from __future__ import annotations

from meiva.impersonation.attachment import Attachment
from meiva.impersonation.attachment import attach as open_attachment

__all_for_meiva__ = []


def attach(session_id: int | str, *, agent_id: int | None = None) -> Attachment:
    """Borrow an active, unexpired agent identity in this local Python process.

    Use the session's integer id together with its owning agent id, or the
    lease handle. Attaching needs no credential: the caller must descend from
    the session's recorded controller process tree, rechecked on every lease
    use. Returns a context manager (`flush`, `close`, `native_snapshot`) that
    loads the agent's saved configuration and plugin state; attaching never
    renews the lease.
    """
    return open_attachment(session_id, agent_id=agent_id)
