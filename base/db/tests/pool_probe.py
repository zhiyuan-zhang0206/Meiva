# meiva:note
# A daemon's unique gate identifies its sessions; the independent baseline-relative
# global check still rejects newly leaked sessions outside that gate. Pool closure is
# not proof of release: a synchronous worker can retain a borrowed connection.
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass, replace
from datetime import datetime
from uuid import uuid4

import psycopg
import pytest
from psycopg.rows import class_row

from base.db import Database
from base.db.code_version_gate import ProcessDbGate
from base.host.exit_codes import hard_exit
from base.native_process.code_version import CodeVersion
from base.native_process.loaded_commit import LoadedCommit


@dataclass(frozen=True)
class Backend:
    pid: int
    backend_start: datetime
    application_name: str
    datname: str
    state: str | None
    wait_event_type: str | None
    wait_event: str | None
    query: str

    @property
    def anchor(self) -> tuple[int, datetime]:
        return self.pid, self.backend_start


def query_shape(query: str) -> str:
    query = re.sub(r"(\$[A-Za-z_][A-Za-z_0-9]*\$|\$\$).*?\1", "?", query, flags=re.S)
    query = re.sub(r"(?:[eE])?'(?:\\.|''|[^'\\])*'", "?", query)
    query = re.sub(r"/\*.*?\*/|--[^\n]*", "", query, flags=re.S)
    return re.sub(r"\b\d+(?:\.\d+)?\b", "?", query)[:500]


class PoolProbe:
    def __init__(self, observer: psycopg.Connection, gate: ProcessDbGate) -> None:
        self.observer = observer
        self.gate = gate
        self.name = gate.application_name()
        assert len(self.name.encode()) < 63, "the unique gate must not be truncated by Postgres"
        self.baseline = self.rows()
        self.baseline_anchors = {row.anchor for row in self.baseline}

    def database(self) -> Database:
        return Database.from_settings(gate=self.gate)

    def rows(self) -> list[Backend]:
        self.observer.rollback()
        with self.observer.cursor(row_factory=class_row(Backend)) as cursor:
            cursor.execute(
                "SELECT pid, backend_start, application_name, datname, state, wait_event_type, "
                "wait_event, query FROM pg_stat_activity "
                "WHERE application_name LIKE 'meiva:%%'"
            )
            return [replace(row, query=query_shape(row.query)) for row in cursor.fetchall()]

    def owned(self, rows: list[Backend]) -> list[Backend]:
        return [row for row in rows if row.application_name == self.name]

    def new_global(self, rows: list[Backend]) -> list[Backend]:
        return [row for row in rows if row.anchor not in self.baseline_anchors]

    def pending(self) -> bool:
        rows = self.rows()
        return bool(self.owned(rows) or self.new_global(rows))

    def diagnostics(self, rows: list[Backend]) -> str:
        tasks = (
            [(task.get_name(), task.done(), task.cancelled()) for task in asyncio.all_tasks()]
            if _running_loop()
            else []
        )
        return repr(
            {
                "application_name": self.name,
                "baseline_global": self.baseline,
                "owned": self.owned(rows),
                "remaining_global": rows,
                "new_global": self.new_global(rows),
                "async_tasks": tasks,
                "pool_object": "not exposed by daemon Database.pool seam",
                "owned_workers": "server states/waits shown in owned rows; Python worker ownership unavailable",
            }
        )

    def assert_owned_released(self) -> None:
        rows = self.rows()
        assert not self.owned(rows), "owned pool connections remain: " + self.diagnostics(rows)

    def assert_global_restored(self) -> None:
        rows = self.rows()
        assert not self.new_global(rows), "new global connections remain: " + self.diagnostics(rows)

    async def wait_released(self, timeout_s: float) -> None:
        deadline = time.monotonic() + timeout_s
        while self.pending() and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        self.assert_released()

    def assert_released(self) -> None:
        self.assert_owned_released()
        self.assert_global_restored()


def _running_loop() -> bool:
    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


@pytest.fixture
def pool_probe(db_conn: psycopg.Connection) -> PoolProbe:
    version = CodeVersion(LoadedCommit.capture())
    gate = ProcessDbGate(
        version=version.get,
        process="test-" + uuid4().hex[:16],
        end_process=hard_exit,
    )
    return PoolProbe(db_conn, gate)
