# meiva:note
# The page-keyed state of the shared browser-mcp daemon: agent selection, the page ledger's
# records and the page-id <-> target map. Its use is described in
# `services.desktop.browser.page_lifecycle`.

from __future__ import annotations

from base.clock import Clock
from services.desktop.browser.tab_groups.page_ledger import LedgerFile, PageRecord
from services.desktop.browser.tab_groups.page_store import (
    RecordStore,
    SelectionStore,
    records_of,
)


# meiva:note
# The page-keyed state of the shared browser-mcp daemon, one per process.
#
# Built once by the daemon's composition root and handed to its `ChromeMcpDaemon`.
# The daemon serves one upstream connection and exits when it dies, so every page
# id here belongs to the upstream process that is still serving.
#
# - `affinity`: agent id -> current page id. The selected-page state belongs to the
#   AGENT, not to a TCP connection: an exec subprocess child re-connecting mid-turn
#   must land on the same tab the agent selected. Requests without an agent id
#   (legacy wrapper clients) keep the per-connection fallback in the daemon.
# - `records`: CDP target id -> `PageRecord`, persisted on every change (see
#   `RecordStore`); the only candidate source for closing, so pages this stack never
#   created (the user's own tabs) are never inspected or closed. `migrate_pending`: the
#   process started with no ledger file, so its first reconciliation runs the one-time
#   migration of pages already in agent groups; any saved file (whoever wrote it first)
#   counts as the migration decided, so it never runs twice.
# - `selected_targets`: agent id -> CDP target id of its selected page, persisted on every
#   change (see `SelectionStore`); the part of `affinity` that survives a daemon restart.
# - `page_targets`: page id -> target id, for the pages this process created or has
#   matched to a target; empty at every daemon start.
# - `due_seen`, `close_failures`: in-memory sweep state (the previous sweep's due
#   targets; consecutive failed closes per target, to keep the log quiet).
class PageRegistry:
    def __init__(
        self,
        default_ttl_seconds: float,
        *,
        clock: Clock,
        store: RecordStore,
        selection_store: SelectionStore,
    ) -> None:
        self.default_ttl_seconds = float(default_ttl_seconds)
        self._clock = clock
        self._store = store
        self._selection_store = selection_store
        self.affinity: dict[int, int | None] = {}
        self.selected_targets: dict[int, str] = selection_store.load()
        loaded = store.load()
        self.records: dict[str, PageRecord] = records_of(loaded)
        self.migrate_pending = loaded is None or not loaded.migrated
        self.page_targets: dict[int, str] = {}
        self.due_seen: set[str] = set()
        self.close_failures: dict[str, int] = {}
        self._grouped_early: set[str] = set()

    def _save(self) -> None:
        self._store.save(LedgerFile(self.records, migrated=True))

    # meiva:note
    # Wall-clock epoch seconds, the unit of every deadline.
    def now(self) -> float:
        return self._clock.now().timestamp()

    def get_agent_page(self, agent_id: int) -> int | None:
        return self.affinity.get(agent_id)

    # meiva:note
    # A page of None also forgets the saved selection: there is nothing to restore.
    def set_agent_page(self, agent_id: int, page_id: int | None) -> None:
        self.affinity[agent_id] = page_id
        if page_id is None:
            self.set_selected_target(agent_id, None)

    def set_selected_target(self, agent_id: int, target_id: str | None) -> None:
        if target_id is None:
            changed = self.selected_targets.pop(agent_id, None) is not None
        else:
            changed = self.selected_targets.get(agent_id) != target_id
            self.selected_targets[agent_id] = target_id
        if changed:
            self._selection_store.save(self.selected_targets)

    # meiva:note
    # Give a freshly created page its record: the owner and `now + default TTL`.
    # Called by the daemon right after a `new_page` it forwarded once the page's
    # target was identified (a page whose target could not be told from a tab
    # someone else opened meanwhile, or that has no agent, carries no record). It
    # starts `grouped` only when the background grouping already reported success.
    def register_created_page(
        self, target_id: str | None, agent_id: int | None, page_id: int | None
    ) -> None:
        if target_id is None or agent_id is None:
            return
        grouped = target_id in self._grouped_early
        self._grouped_early.discard(target_id)
        self.records[target_id] = PageRecord(
            agent_id, self.now() + self.default_ttl_seconds, grouped=grouped
        )
        if page_id is not None:
            self.page_targets[page_id] = target_id
        self._save()

    # meiva:note
    # The background grouping filed the page into its owner's group.
    def mark_grouped(self, target_id: str) -> None:
        record = self.records.get(target_id)
        if record is None:
            self._grouped_early.add(target_id)
        elif not record.grouped:
            self.records[target_id] = PageRecord(
                record.owner, record.expires_at, grouped=True, url=record.url
            )
            self._save()

    def record_for_page(self, page_id: int) -> PageRecord | None:
        target_id = self.page_targets.get(page_id)
        return self.records.get(target_id) if target_id is not None else None

    def page_of_target(self, target_id: str) -> int | None:
        for page_id, known in self.page_targets.items():
            if known == target_id:
                return page_id
        return None

    # meiva:note
    # Adopt a reconciled record set, writing the file only when something changed.
    def replace_state(self, records: dict[str, PageRecord], *, migration_ran: bool) -> None:
        if records != self.records or migration_ran:
            self.records = records
            self._save()
        if migration_ran:
            self.migrate_pending = False

    def set_deadline(self, target_id: str, expires_at: float) -> None:
        record = self.records[target_id]
        self.records[target_id] = PageRecord(
            record.owner, expires_at, grouped=record.grouped, url=record.url
        )
        self._save()

    # meiva:note
    # Drop the record of a page that is closed (or was never ours).
    def forget_target(self, target_id: str) -> None:
        page_id = self.page_of_target(target_id)
        if page_id is not None:
            del self.page_targets[page_id]
        self.close_failures.pop(target_id, None)
        if self.records.pop(target_id, None) is not None:
            self._save()

    # meiva:note
    # Forget the record of a page closed through the upstream's `close_page`.
    def drop_page_ttl(self, page_id: int | None) -> None:
        target_id = self.page_targets.get(page_id) if page_id is not None else None
        if target_id is not None:
            self.forget_target(target_id)

    # meiva:note
    # Clear every agent slot naming `page_id` (the page is gone); returns one
    # cleared affinity owner for logs. A slot left naming a closed page would make
    # the owner's next call re-pin against a dead id first.
    def clear_affinity_for_page(self, page_id: int) -> int | None:
        owner: int | None = None
        for agent_id, current in list(self.affinity.items()):
            if current == page_id:
                self.set_agent_page(agent_id, None)
                if owner is None:
                    owner = agent_id
        return owner
