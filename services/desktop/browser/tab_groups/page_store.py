from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Protocol

from base.host.atomic_io import write_text_atomic
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.log import logger
from base.paths import run_dir
from services.desktop.browser.tab_groups.page_ledger import (
    LEDGER_VERSION,
    LedgerFile,
    PageRecord,
)

SELECTION_VERSION = 1


def dump_records(file: LedgerFile) -> str:
    payload = {
        "version": LEDGER_VERSION,
        "migrated": file.migrated,
        "pages": {
            target_id: {
                "owner": record.owner,
                "expires_at": record.expires_at,
                "grouped": record.grouped,
                "url": record.url,
                "adopted": record.adopted,
            }
            for target_id, record in sorted(file.records.items())
        },
    }
    return json.dumps(payload, indent=2) + "\n"


def _parse_record(target_id: str, entry: object) -> PageRecord:
    if not target_id or not is_object_dict(entry):
        raise ValueError(f"malformed ledger entry for {target_id!r}")
    owner, expires_at = entry.get("owner"), entry.get("expires_at")
    grouped, url = entry.get("grouped"), entry.get("url")
    if isinstance(owner, bool) or not isinstance(owner, int) or owner < 0:
        raise ValueError(f"malformed owner for {target_id!r}")
    if (
        isinstance(expires_at, bool)
        or not isinstance(expires_at, int | float)
        or not math.isfinite(expires_at)
    ):
        raise ValueError(f"malformed expires_at for {target_id!r}")
    adopted = entry.get("adopted")
    if not isinstance(grouped, bool) or not isinstance(url, str) or not isinstance(adopted, bool):
        raise TypeError(f"malformed grouped/url/adopted for {target_id!r}")
    return PageRecord(owner, float(expires_at), grouped=grouped, url=url, adopted=adopted)


# meiva:note
# The ledger a file holds. Raises ValueError/TypeError for anything not exactly the
# shape `dump_records` writes: a partly trusted ledger is worse than none.
def parse_records(text: str) -> LedgerFile:
    raw = object_of(json.loads, text)
    if not is_object_dict(raw) or raw.get("version") != LEDGER_VERSION:
        raise ValueError("not a version-1 page ledger")
    migrated, pages = raw.get("migrated"), raw.get("pages")
    if not isinstance(migrated, bool):
        raise TypeError("'migrated' must be a boolean")
    if not is_object_dict(pages):
        raise ValueError("'pages' must be an object")
    return LedgerFile(
        {target_id: _parse_record(target_id, entry) for target_id, entry in pages.items()},
        migrated,
    )


# meiva:note
# Where the page ledger lives between daemon processes. `load` is None when no
# ledger exists yet (the first run of the mechanism, which is when the one-time
# migration of pages already in agent groups happens).
class RecordStore(Protocol):
    def load(self) -> LedgerFile | None: ...

    def save(self, file: LedgerFile) -> None: ...


# meiva:note
# The page ledger file of a home (`<home>/run/browser-pages.json`), beside the
# browser session ledger (`browser-session.json`): runtime state that survives a
# daemon restart and is read by no other process.
def page_ledger_path(home: Path) -> Path:
    return run_dir(home) / "browser-pages.json"


def records_of(file: LedgerFile | None) -> dict[str, PageRecord]:
    return dict(file.records) if file is not None else {}


# meiva:note
# The page records as one JSON file, replaced atomically. A file that cannot be
# read or parsed is not an error: it reads as no records with the migration marked
# done (a WARNING says so) -- an unreadable ledger is not evidence that the pages in
# agent groups are the stack's, so nothing is adopted from it. A failed save is a
# WARNING too: the in-memory records stay right.
class FileRecordStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> LedgerFile | None:
        try:
            return parse_records(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError, TypeError) as e:
            logger.warning(
                "[browser-mcp] ignoring unreadable page ledger {} ({}: {}); no page is "
                "adopted from agent tab groups because of it",
                self._path,
                type(e).__name__,
                e,
            )
            return LedgerFile({}, migrated=True)

    def save(self, file: LedgerFile) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(self._path, dump_records(file), mode=0o600)
        except OSError as e:
            logger.warning(
                "[browser-mcp] could not write the page ledger {} ({}: {}); deadlines are kept "
                "in memory only until the next successful write",
                self._path,
                type(e).__name__,
                e,
            )


# meiva:note
# The agents' selected pages (agent id -> CDP target id) between daemon processes. The
# upstream's page numbers restart with it; the target id is what survives. Selection is
# presentation state, not authority: it never says a page may be closed (only the page
# ledger does), so an unreadable file reads as no selections and a failed save is a WARNING.
class SelectionStore(Protocol):
    def load(self) -> dict[int, str]: ...

    def save(self, selected: dict[int, str]) -> None: ...


def selection_path(home: Path) -> Path:
    return run_dir(home) / "browser-selected-pages.json"


def dump_selection(selected: dict[int, str]) -> str:
    payload = {
        "version": SELECTION_VERSION,
        "selected": {str(agent_id): target_id for agent_id, target_id in sorted(selected.items())},
    }
    return json.dumps(payload, indent=2) + "\n"


# meiva:note
# Raises ValueError/TypeError for anything not exactly the shape `dump_selection` writes.
def parse_selection(text: str) -> dict[int, str]:
    raw = object_of(json.loads, text)
    if not is_object_dict(raw) or raw.get("version") != SELECTION_VERSION:
        raise ValueError("not a version-1 page selection file")
    selected = raw.get("selected")
    if not is_object_dict(selected):
        raise ValueError("'selected' must be an object")
    parsed: dict[int, str] = {}
    for agent, target_id in selected.items():
        if not agent.isascii() or not agent.isdigit() or not isinstance(target_id, str):
            raise TypeError(f"malformed selection entry for {agent!r}")
        if not target_id:
            raise ValueError(f"empty target id for agent {agent!r}")
        parsed[int(agent)] = target_id
    return parsed


class FileSelectionStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> dict[int, str]:
        try:
            return parse_selection(self._path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError, TypeError) as e:
            logger.warning(
                "[browser-mcp] ignoring unreadable page selection file {} ({}: {}); agents "
                "re-select their pages",
                self._path,
                type(e).__name__,
                e,
            )
            return {}

    def save(self, selected: dict[int, str]) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            write_text_atomic(self._path, dump_selection(selected), mode=0o600)
        except OSError as e:
            logger.warning(
                "[browser-mcp] could not write the page selection file {} ({}: {}); selections "
                "are kept in memory only until the next successful write",
                self._path,
                type(e).__name__,
                e,
            )
