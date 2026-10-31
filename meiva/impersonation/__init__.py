"""Attach an external controller to an active Meiva takeover session."""

from __future__ import annotations

from types import TracebackType
from typing import Any, Protocol, Self

__all_for_meiva__ = []


# meiva:note
# `native_snapshot()` returns the agent's saved state, and the annotation would like to say so by
# naming `base.agents.context.external_attachment.state_schema.BaseAgentState`. It stays `Any`
# because that module imports `langchain_core.messages` (`AnyMessage`) and `import meiva` reaches
# this package: measured 2026-10-31, naming the type pulled 345 langchain / langgraph / langsmith
# modules into a bare `import meiva` and turned nine lazy-import guards red (three under
# `meiva/tests/bootstrap`, four under `agent/tests/execution/test_lazy_child_imports.py`, one in
# `meiva_fleet`'s lazy-plugin test, one in `meiva_memory`'s subprocess-import test). The
# reader that needs the type -- the attachment -- imports the schema itself in `attachment.py`,
# when `attach()` runs.
class _Attachment(Protocol):
    agent_id: int
    session_id: int
    lease_id: str
    _event_gate: object

    def flush(self) -> None: ...

    def close(self) -> None: ...

    def native_snapshot(self) -> Any: ...

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
