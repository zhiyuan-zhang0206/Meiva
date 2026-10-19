from __future__ import annotations

import asyncio
import json
import socket
import time
from typing import Any

from services.desktop.browser import page_lifecycle
from services.desktop.browser.mcp_daemon import ChromeMcpDaemon
from services.desktop.browser.tab_groups.grouping import AgentTabGroups
from services.desktop.browser.tests.test_browser_mcp_daemon import (
    FakeUpstream,
    err,
    new_daemon,
    text_of,
)


class RecordingGroups(AgentTabGroups):
    def __init__(self) -> None:
        super().__init__(cdp_port=0, tasks=asyncio.TaskGroup())
        self.events: list[tuple[str, Any]] = []
        self.snapshots = 0

    async def snapshot(self) -> frozenset[str] | None:
        self.snapshots += 1
        self.events.append(("snapshot", self.snapshots))
        return frozenset({f"before-{self.snapshots}"})

    async def group_new_page(
        self, agent_id: int, before: frozenset[str] | None, page_id: int | None = None
    ) -> None:
        self.events.append(("group", (agent_id, before)))

    async def release_page(self, page_id: int) -> None:
        self.events.append(("release", page_id))

    def grouped(self) -> list[int]:
        return [payload[0] for kind, payload in self.events if kind == "group"]


def _grouped_daemon() -> tuple[ChromeMcpDaemon, FakeUpstream, RecordingGroups]:
    up = FakeUpstream()
    groups = RecordingGroups()
    return new_daemon(up, tab_groups=groups), up, groups


async def test_explicit_new_page_is_grouped_around_the_upstream_call() -> None:
    d, up, groups = _grouped_daemon()
    result = await d.call_tool_for_agent("new_page", {"url": "https://a.example/"}, 7)
    assert not result.is_error
    assert groups.events == [("snapshot", 1), ("group", (7, frozenset({"before-1"})))]
    assert [c[0] for c in up.calls] == ["new_page"]


async def test_first_navigate_auto_created_page_is_grouped() -> None:
    d, up, groups = _grouped_daemon()
    result = await d.call_tool_for_agent("navigate_page", {"url": "https://a.example/"}, 7)
    assert not result.is_error
    assert up.pages == {1: "https://a.example/"}
    assert groups.grouped() == [7]
    assert 1 in d.pages.ttl_deadlines


async def test_one_agent_many_pages_and_two_agents_are_told_apart_by_agent_id() -> None:
    d, _up, groups = _grouped_daemon()
    await d.call_tool_for_agent("new_page", {"url": "a"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "b"}, 8)
    await d.call_tool_for_agent("new_page", {"url": "c"}, 7)
    assert groups.grouped() == [7, 8, 7]


async def test_only_pages_the_stack_creates_are_grouped() -> None:
    d, up, groups = _grouped_daemon()
    up.pages[99] = "https://user.example/tab"
    up.next_id = 100
    await d.call_tool_for_agent("select_page", {"pageId": 99}, 7)
    await d.call_tool_for_agent("take_snapshot", {}, 7)
    await d.call_tool_for_agent("navigate_page", {"url": "https://user.example/other"}, 7)
    await d.call_tool_for_agent("list_pages", {}, 7)
    assert groups.events == []


async def test_isolated_context_page_is_left_out_of_grouping() -> None:
    d, up, groups = _grouped_daemon()
    result = await d.call_tool_for_agent(
        "new_page", {"url": "https://discord.com/app", "isolatedContext": "reg-7"}, 7
    )
    assert not result.is_error
    assert up.isolated_contexts == {1: "reg-7"}
    assert groups.events == []
    assert 1 in d.pages.ttl_deadlines


async def test_a_failed_new_page_is_not_grouped() -> None:
    class Failing(FakeUpstream):
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            if name == "new_page":
                return err("net::ERR_NAME_NOT_RESOLVED")
            return await super().call_tool(name, args)

    groups = RecordingGroups()
    d = new_daemon(Failing(), tab_groups=groups)
    result = await d.call_tool_for_agent("new_page", {"url": "https://nope.invalid/"}, 7)
    assert result.is_error
    assert groups.grouped() == []
    assert d.pages.ttl_deadlines == {}


async def test_agentless_legacy_connection_is_not_grouped() -> None:
    d, _up, groups = _grouped_daemon()
    await d.call_tool("new_page", {"url": "a"}, None)
    await d.call_tool("navigate_page", {"url": "b"}, None)
    assert groups.events == []


def _closed_local_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_grouping_failure_leaves_the_page_created_registered_and_selected(
    loguru_records: list[dict[str, Any]],
) -> None:
    up = FakeUpstream()
    d = new_daemon(up, tab_groups=AgentTabGroups(_closed_local_port(), asyncio.TaskGroup()))

    explicit = await d.call_tool_for_agent("new_page", {"url": "https://a.example/"}, 7)
    auto = await d.call_tool_for_agent("navigate_page", {"url": "https://b.example/"}, 8)

    assert not explicit.is_error and not auto.is_error
    assert text_of(explicit) == "  1: https://a.example/ [selected]"
    assert d.pages.get_agent_page(7) == 1
    assert d.pages.get_agent_page(8) == 2
    assert set(d.pages.ttl_deadlines) == {1, 2}
    warnings = [r for r in loguru_records if r["level"].name == "WARNING"]
    assert warnings, "a grouping failure must leave a trace"
    assert all("tab group" in r["message"] for r in warnings)


class WedgedChrome:
    def __init__(self) -> None:
        self.listings = 0
        self.server: asyncio.Server | None = None
        self.port = 0
        self._writers: list[asyncio.StreamWriter] = []

    async def __aenter__(self) -> WedgedChrome:
        self.server = await asyncio.start_server(self._serve, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc: object) -> None:
        assert self.server is not None
        self.server.close()
        for writer in self._writers:
            writer.close()
        await self.server.wait_closed()

    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._writers.append(writer)
        request = await reader.readuntil(b"\r\n\r\n")
        path = request.split()[1].decode()
        if path == "/json/list":
            self.listings += 1
            targets = [{"id": "T1", "type": "page"}]
            if self.listings > 1:
                targets.append({"id": "T2", "type": "page"})
            body = json.dumps(targets)
        elif path == "/json/version":
            body = json.dumps(
                {"webSocketDebuggerUrl": f"ws://127.0.0.1:{self.port}/devtools/browser/x"}
            )
        else:
            await asyncio.Event().wait()
            return
        head = (
            f"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: {len(body)}\r\n"
        )
        writer.write(f"{head}Connection: close\r\n\r\n{body}".encode())
        await writer.drain()
        writer.close()


async def test_a_stuck_grouping_neither_delays_new_page_nor_holds_the_daemon_lock() -> None:
    async with WedgedChrome() as chrome, asyncio.TaskGroup() as tasks:
        groups = AgentTabGroups(chrome.port, tasks)
        d = new_daemon(FakeUpstream(), tab_groups=groups)
        try:
            started = time.monotonic()
            created = await asyncio.wait_for(
                d.call_tool_for_agent("new_page", {"url": "https://a.example/"}, 7), timeout=3
            )
            other = await asyncio.wait_for(d.call_tool_for_agent("list_pages", {}, 8), timeout=1)
            assert not created.is_error and not other.is_error
            assert time.monotonic() - started < 3
            assert chrome.listings == 2
            stuck = [t for t in asyncio.all_tasks() if t.get_name() == "tab-group-agent-7"]
            assert len(stuck) == 1 and not stuck[0].done()
        finally:
            groups.cancel_pending()


class _CloseObserver(FakeUpstream):
    def closed_pages(self) -> list[int]:
        return [args["pageId"] for name, args, _sel in self.calls if name == "close_page"]


async def _agent_with_one_page(
    groups: RecordingGroups | None = None,
) -> tuple[ChromeMcpDaemon, _CloseObserver]:
    up = _CloseObserver()
    d = new_daemon(up, tab_groups=groups or RecordingGroups())
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:9/dev"}, 7)
    up.calls.clear()
    return d, up


async def test_release_closes_the_agents_last_page_through_close_page() -> None:
    d, up = await _agent_with_one_page()
    await d.call_tool_for_agent("new_page", {"url": "http://localhost:9/dev-2"}, 7)
    up.calls.clear()
    assert await page_lifecycle.release_agent_page(d, 7) == 2
    assert up.closed_pages() == [1, 2]


async def test_hard_ttl_reaper_closes_through_close_page() -> None:
    d, up = await _agent_with_one_page()
    assert 1 in d.pages.ttl_deadlines
    d.pages.ttl_deadlines[1] = time.monotonic() - 1.0
    assert await page_lifecycle.reap_expired_pages(d) == 1
    assert up.closed_pages() == [1]


async def test_the_public_surface_is_the_grouping_contract_and_one_ungroup() -> None:
    # meiva:note
    # `release_page` is the one call that can destroy a group, and it exists
    # because Chrome saves a group whose last tab closes: without it every close
    # leaves a saved group that the title lookup can no longer see. The decision
    # about WHICH tab may be ungrouped stays in the extension (`meivaUngroupLast`
    # checks the title and the single member), so this side never names a group.
    # Keep the surface at four names; a new helper here would be a second write
    # path to reconcile.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    public = {n for n in dir(AgentTabGroups) if not n.startswith("_")}
    assert public == {"snapshot", "group_new_page", "release_page", "cancel_pending"}


async def test_close_page_ungroups_the_last_tab_before_the_upstream_closes_it() -> None:
    # meiva:note
    # The ungroup must land BEFORE the close: Chrome saves a group whose last tab
    # closes, and a saved group can no longer be reached.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    order: list[str] = []

    class OrderedUpstream(FakeUpstream):
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            if name == "close_page":
                order.append("close_page")
            return await super().call_tool(name, args)

    class OrderedGroups(RecordingGroups):
        async def release_page(self, page_id: int) -> None:
            order.append(f"release:{page_id}")
            await super().release_page(page_id)

    groups = OrderedGroups()
    d = new_daemon(OrderedUpstream(), tab_groups=groups)
    await d.call_tool_for_agent("new_page", {"url": "https://a.example/"}, 7)

    result = await d.call_tool_for_agent("close_page", {"pageId": 1}, 7)

    assert not result.is_error
    assert order == ["release:1", "close_page"], order


async def test_release_of_an_agents_pages_also_ungroups_each_before_closing() -> None:
    # meiva:note
    # `release_agent_page` (the termination hook) closes through the same door.
    # meiva:link services/desktop/browser/docs/tab-groups.meiva.okf.md
    order: list[str] = []

    class OrderedUpstream(FakeUpstream):
        async def call_tool(self, name: str, args: dict[str, Any]) -> Any:
            if name == "close_page":
                order.append("close_page")
            return await super().call_tool(name, args)

    class OrderedGroups(RecordingGroups):
        async def release_page(self, page_id: int) -> None:
            order.append(f"release:{page_id}")
            await super().release_page(page_id)

    groups = OrderedGroups()
    d = new_daemon(OrderedUpstream(), tab_groups=groups)
    await d.call_tool_for_agent("new_page", {"url": "https://a.example/"}, 7)
    await d.call_tool_for_agent("new_page", {"url": "https://b.example/"}, 7)

    await page_lifecycle.release_agent_page(d, 7)

    assert order == ["release:1", "close_page", "release:2", "close_page"], order
