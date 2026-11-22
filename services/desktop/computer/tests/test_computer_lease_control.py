from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx

from base.agents.messages.kwargs import NoteTag
from base.clock import Clock, ClockConfig
from base.db import Database, create_agent
from base.host.boundary.json_values import is_object_dict
from base.host.permissions_helper.wire import ClickResult
from services.desktop.computer import mcp_daemon, session
from services.desktop.computer.tests.slices import (
    FakeDesktopHelper,
    computer_use_config,
    text_block,
)
from services.desktop.remote import CornerFailsafeError, RemoteWinBridgeHelper


class _SteppableTimers:
    def __init__(self, *, wall_start: datetime) -> None:
        self._now = 0.0
        self._wall_start = wall_start
        self._waiters: list[tuple[float, asyncio.Future[None]]] = []

    def monotonic(self) -> float:
        return self._now

    def now(self) -> datetime:
        return self._wall_start + timedelta(seconds=self._now)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)

    async def asleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        fut: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self._waiters.append((self._now + seconds, fut))
        await fut

    def advance(self, seconds: float) -> None:
        self._now += seconds
        pending: list[tuple[float, asyncio.Future[None]]] = []
        for deadline, fut in self._waiters:
            if fut.cancelled():
                continue
            if self._now >= deadline and not fut.done():
                fut.set_result(None)
            elif not fut.done():
                pending.append((deadline, fut))
        self._waiters = pending


def _clock_for(timers: _SteppableTimers) -> Clock:
    return Clock(ClockConfig("UTC", "UTC", False), timers=timers, now=timers.now)


def _create_real_agent(database: Database) -> int:
    with database.write_transaction() as conn:
        return create_agent(conn)


def _fetch_system_notes(
    database: Database, agent_id: int
) -> list[tuple[str, str, dict[str, object]]]:
    with database.connect() as conn:
        rows = conn.execute(
            "SELECT kind, content, payload FROM inbound_messages "
            "WHERE agent_id = %s AND kind = 'system_note' ORDER BY id",
            (agent_id,),
        ).fetchall()
    return [(str(r[0]), str(r[1]), dict(r[2])) for r in rows]


async def _call_ok(
    daemon: mcp_daemon.ComputerMcpDaemon,
    req_id: int,
    tool: str,
    args: dict[str, object],
    agent_id: int,
) -> dict[str, object]:
    resp = await daemon._dispatch(
        {"id": req_id, "method": "call_tool", "tool": tool, "args": args, "agent_id": agent_id}
    )
    assert resp["ok"] is True
    res = resp["result"]
    assert is_object_dict(res)
    parsed: object = json.loads(text_block(res))
    assert is_object_dict(parsed)
    return parsed


async def _flush_and_wait(daemon: mcp_daemon.ComputerMcpDaemon) -> None:
    _ = await daemon._targets.flush_due_reminders(db=daemon._db, now_fn=daemon._clock.now)


def _make_ssh_lease_client(remote_leases: list[dict[str, object]]) -> httpx.Client:
    def mock_handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        tool = str(body["params"]["name"])
        args = dict(body["params"]["arguments"])
        if tool == "lease_manage":
            remote_leases.append(args)
            payload: dict[str, object] = {"ok": True, "released": True}
        else:
            payload = {"ok": True}
        rpc = {
            "jsonrpc": "2.0",
            "id": body["id"],
            "result": {
                "content": [{"type": "text", "text": json.dumps(payload)}],
                "isError": False,
            },
        }
        return httpx.Response(200, json=rpc)

    return httpx.Client(transport=httpx.MockTransport(mock_handler))


async def test_default_600s_lease_and_180s_in_band_and_out_of_band_reminders(
    database: Database, tmp_path: Path
) -> None:
    agent_id = _create_real_agent(database)
    timers = _SteppableTimers(wall_start=datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC))
    daemon = mcp_daemon.ComputerMcpDaemon(
        computer_use_config(computer_use_lease_s=600.0),
        database,
        str(tmp_path / "lease.sock"),
        helper=FakeDesktopHelper(),
        clock=_clock_for(timers),
        home=tmp_path,
        local_machine="ubuntu",
    )
    stop = asyncio.Event()
    try:
        assert session.DEFAULT_LEASE_S == 600.0
        async with asyncio.TaskGroup() as tg:
            task = tg.create_task(daemon.run_reminder_loop(stop))
            first_data = await _call_ok(daemon, 1, "click", {"x": 10, "y": 20}, agent_id)
            assert "lease_reminder" not in first_data
            assert daemon._state_for("local").screen.holder_ttl_s == 600.0

            timers.advance(419.0)
            await _flush_and_wait(daemon)
            assert _fetch_system_notes(database, agent_id) == []

            timers.advance(1.0)
            await _flush_and_wait(daemon)
            notes = _fetch_system_notes(database, agent_id)
            assert len(notes) == 1
            assert notes[0][0] == "system_note"
            assert notes[0][2] == {"note_tag": NoteTag.SDK_HINT.value}
            assert "180s" in notes[0][1] and "acquire_control" in notes[0][1]

            timers.advance(60.0)
            second_data = await _call_ok(daemon, 2, "click", {"x": 15, "y": 25}, agent_id)
            assert second_data["lease_remaining_seconds"] == 120
            assert "acquire_control" in str(second_data["lease_reminder"])
            stop.set()
            _ = task.cancel()
    finally:
        daemon.close()


async def test_acquire_control_syncs_local_and_ssh_ttl_and_reschedules_reminder(
    database: Database, tmp_path: Path
) -> None:
    agent_id = _create_real_agent(database)
    _ = (tmp_path / "remote_hosts.toml").write_text(
        '[hosts.zzyasus]\nos = "windows"\nlocal_port = 18792\n', encoding="utf-8"
    )
    timers = _SteppableTimers(wall_start=datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC))
    remote_leases: list[dict[str, object]] = []
    http_client = _make_ssh_lease_client(remote_leases)
    daemon = mcp_daemon.ComputerMcpDaemon(
        computer_use_config(computer_use_lease_s=600.0),
        database,
        str(tmp_path / "ssh-lease.sock"),
        helper=None,
        clock=_clock_for(timers),
        home=tmp_path,
        local_machine="ubuntu",
        remote_helper_factory=lambda _cfg, aid: RemoteWinBridgeHelper(
            "http://127.0.0.1:18792",
            holder=f"meiva-agent-{aid or 0}",
            lease_seconds=600,
            timers=timers,
            client=http_client,
        ),
    )
    try:
        d1 = await _call_ok(daemon, 1, "acquire_control", {"host": "ssh://zzyasus"}, agent_id)
        assert (d1["ttl_seconds"], d1["lease_remaining_seconds"]) == (600, 600)
        assert d1["lease_expires_at"] == "2026-11-22T12:10:00Z"
        assert (remote_leases[-1]["action"], remote_leases[-1]["ttl_seconds"]) == ("acquire", 600)

        timers.advance(300.0)
        d2 = await _call_ok(
            daemon, 2, "acquire_control", {"host": "ssh://zzyasus", "ttl_seconds": 1200}, agent_id
        )
        assert (d2["ttl_seconds"], d2["lease_remaining_seconds"]) == (1200, 1200)
        assert d2["lease_expires_at"] == "2026-11-22T12:25:00Z"
        assert (remote_leases[-1]["action"], remote_leases[-1]["ttl_seconds"]) == ("acquire", 1200)

        timers.advance(150.0)
        await _flush_and_wait(daemon)
        assert _fetch_system_notes(database, agent_id) == []

        timers.advance(870.0)
        await _flush_and_wait(daemon)
        notes = _fetch_system_notes(database, agent_id)
        assert len(notes) == 1
        assert "ssh://zzyasus" in notes[0][1]
    finally:
        daemon.close()


class _AbortingHelper(FakeDesktopHelper):
    def click(
        self,
        x: float,
        y: float,
        *,
        double: bool = False,
        button: str = "left",
        click_count: int | None = None,
        modifiers: list[str] | None = None,
        duration_ms: float | None = None,
    ) -> ClickResult:
        del x, y, double, button, click_count, modifiers, duration_ms
        raise CornerFailsafeError("corner abort")


async def test_early_release_and_corner_abort_cancel_pending_reminders(
    database: Database, tmp_path: Path
) -> None:
    agent_release = _create_real_agent(database)
    agent_abort = _create_real_agent(database)
    timers = _SteppableTimers(wall_start=datetime(2026, 11, 22, 12, 0, 0, tzinfo=UTC))
    clock = _clock_for(timers)
    daemon = mcp_daemon.ComputerMcpDaemon(
        computer_use_config(computer_use_lease_s=600.0),
        database,
        str(tmp_path / "cancel.sock"),
        helper=FakeDesktopHelper(),
        clock=clock,
        home=tmp_path,
        local_machine="ubuntu",
    )
    daemon_abort = mcp_daemon.ComputerMcpDaemon(
        computer_use_config(computer_use_lease_s=600.0),
        database,
        str(tmp_path / "abort.sock"),
        helper=_AbortingHelper(),
        clock=clock,
        home=tmp_path,
        local_machine="ubuntu",
    )
    try:
        _ = await _call_ok(daemon, 1, "acquire_control", {"ttl_seconds": 600}, agent_release)
        _ = await _call_ok(daemon, 2, "release_control", {}, agent_release)

        _ = await _call_ok(daemon_abort, 3, "acquire_control", {"ttl_seconds": 600}, agent_abort)
        aborted = await daemon_abort._dispatch(
            {
                "id": 4,
                "method": "call_tool",
                "tool": "click",
                "args": {"x": 5, "y": 5},
                "agent_id": agent_abort,
            }
        )
        assert aborted["ok"] is False

        timers.advance(500.0)
        await _flush_and_wait(daemon)
        await _flush_and_wait(daemon_abort)
        assert _fetch_system_notes(database, agent_release) == []
        assert _fetch_system_notes(database, agent_abort) == []
    finally:
        daemon.close()
        daemon_abort.close()
