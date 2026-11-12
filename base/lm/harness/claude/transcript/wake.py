from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

# meiva:note
# The `session_store` the Claude SDK is given so the CLI's transcript mirror can wake the timeline writer. The
# mirror's rows are never parsed or used: `append` only says that a record grew (the key's `subpath` names a
# sub-agent record) and returns, so a slow or failing writer can never fail the SDK's mirror; `load` always
# answers "never written", which keeps resume on the CLI's own file instead of a temporary config directory
# the SDK would materialize from a store that returned entries. The writer reads the file by position.
# meiva:link base/lm/harness/docs/transcript-projection.md


class WakeSessionStore:
    def __init__(self, wake: Callable[[str | None], None]) -> None:
        self._wake = wake

    async def append(self, key: Mapping[str, str], entries: Sequence[object]) -> None:
        del entries
        self._wake(key.get("subpath"))

    async def load(self, key: Mapping[str, str]) -> None:
        del key
