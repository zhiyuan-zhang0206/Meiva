# meiva:note
# Use the serving gateway's CLI for paginated observation and explicit message provenance.


from __future__ import annotations

import shlex
from typing import Protocol

from ..plan.host_commands import HOME, MEIVA
from ..session import RefusedError
from .plan import directory_page, initiator_id


class HostCommands(Protocol):
    def run(
        self, alias: str, script: str, *, effect: bool = True, stdin: str | None = None
    ) -> str: ...


def running_agents(s: HostCommands, gateway: str) -> list[int]:
    agents: list[int] = []
    cursor = None
    seen: set[int] = set()
    while True:
        flag = f" --before-id {cursor}" if cursor is not None else ""
        page = s.run(
            gateway, f"{HOME}; {MEIVA} agents ls --scope live --limit 200{flag}", effect=False
        )
        try:
            running, cursor = directory_page(page)
        except ValueError as exc:
            raise RefusedError(str(exc)) from exc
        agents.extend(running)
        if cursor is None:
            return agents
        if cursor in seen:
            raise ValueError("agent directory cursor did not advance")
        seen.add(cursor)


def notify(
    s: HostCommands,
    gateway: str,
    targets: list[int],
    source: str,
    message: str,
    *,
    delivered_to: set[int] | None = None,
) -> None:
    for target in sorted(set(targets)):
        s.run(
            gateway,
            f"{HOME}; {MEIVA} agents send {target} {shlex.quote(message)} --source {shlex.quote(source)}",
        )
        if delivered_to is not None:
            delivered_to.add(target)


def require_quiescent(
    s: HostCommands,
    gateway: str,
    source: str,
    operation_id: str,
    *,
    delivered_to: set[int] | None = None,
) -> None:
    running = running_agents(s, gateway)
    if running:
        own = initiator_id(source)
        message = (
            f"Scheduled cluster update {operation_id} by {source} aborted before stop: "
            f"running agents {running}; coordinate another attempt."
        )
        notify(
            s,
            gateway,
            running + ([own] if own is not None else []),
            source,
            message,
            delivered_to=delivered_to,
        )
        raise RefusedError(message)
