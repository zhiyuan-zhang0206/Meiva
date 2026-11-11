from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from base.lm.harness.codex.frames import as_object, as_text
from base.lm.harness.session import BackgroundItem, BackgroundKind, describe

# meiva:note
# The commands a Codex thread has started and not seen finish, read from `item/started` and
# `item/completed` for `commandExecution` items. A command the model starts as a long-running
# `unified_exec` session outlives its turn: the turn completes with the item still `inProgress`, and
# the app-server keeps the process until it exits. Only the sources that start a command count
# (`agent`, `unifiedExecStartup`); a user shell command and a poll or write to a session already
# running are interactions, not new work. A turn that ended interrupted or failed took its commands
# with it (the interrupt sweeps them), so its open items are dropped rather than kept as ghosts.
# The app-server does emit `item/completed` for such a command when it ends after its turn
# (recorded on Codex 0.166: status completed, about 37 s after the turn for a 40 s sleep); the session
# still checks the process tree before it reports an item as pending, so an item whose completion
# never arrives cannot keep a session alive.
# meiva:link base/lm/harness/codex/docs/codex.md
_COUNTED_SOURCES = frozenset({"agent", "unifiedExecStartup"})
_COMMAND_LIMIT = 100


@dataclass(frozen=True, slots=True)
class OpenCommands:
    entries: tuple[tuple[str, str | None, BackgroundItem], ...] = ()

    def seen(
        self, phase: str, turn_name: str | None, payload: Mapping[str, object]
    ) -> OpenCommands:
        item = as_object(payload)
        if as_text(item.get("type")) != "commandExecution":
            return self
        item_id = as_text(item.get("id"))
        if not item_id:
            return self
        rest = tuple(entry for entry in self.entries if entry[0] != item_id)
        if phase == "completed":
            return OpenCommands(rest)
        status = as_text(item.get("status"))
        if status not in (None, "inProgress") or as_text(item.get("source") or "agent") not in (
            _COUNTED_SOURCES
        ):
            return OpenCommands(rest)
        command = describe(as_text(item.get("command")) or "", limit=_COMMAND_LIMIT)
        label = BackgroundItem(BackgroundKind.COMMAND, command or "command")
        return OpenCommands((*rest, (item_id, turn_name, label)))

    def turn_ended(self, turn_name: str | None, *, survived: bool) -> OpenCommands:
        if survived:
            return self
        return OpenCommands(tuple(entry for entry in self.entries if entry[1] != turn_name))

    @property
    def items(self) -> tuple[BackgroundItem, ...]:
        return tuple(entry[2] for entry in self.entries)
