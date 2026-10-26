"""Attach an external controller to an active Meiva takeover session."""

from __future__ import annotations

from types import TracebackType
from typing import Protocol, Self

from pydantic import BaseModel

__all_for_meiva__ = []


class _Attachment(Protocol):
    agent_id: int
    session_id: int
    lease_id: str
    _event_gate: object

    def flush(self) -> None: ...

    def close(self) -> None: ...

    def native_snapshot(self) -> BaseModel: ...

    def __enter__(self) -> Self: ...

    def __exit__(
        self,
        _kind: type[BaseException] | None,
        _error: BaseException | None,
        _trace: TracebackType | None,
    ) -> None: ...


def attach(session_id: int | str, *, agent_id: int | None = None) -> _Attachment:
    """Borrow an active, unexpired agent identity in this local Python process.

    Use the session's integer id together with its owning agent id, or the
    lease handle. Attaching needs no credential: the caller must descend from
    the session's recorded controller process tree, rechecked on every lease
    use. Returns a context manager (`flush`, `close`, `native_snapshot`) that
    loads the agent's saved configuration and plugin state; attaching never
    renews the lease.
    """
    from meiva.impersonation.attachment import attach as open_attachment

    return open_attachment(session_id, agent_id=agent_id)
