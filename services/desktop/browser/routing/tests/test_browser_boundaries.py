import asyncio
import os
from itertools import count
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent

from base.agents.context.browser_identity import (
    PURPOSE,
    BrowserAuthority,
    hosted_browser_identity,
    serve_browser_authority,
)
from base.clock.tests.fakes import system_clock
from base.host.remote_hosts import RemoteHostConfig
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.native_process.root_control.tests.status_root import serve
from base.paths import run_dir
from services.desktop.browser.routing.remote_files import CallerFiles
from services.desktop.browser.routing.ssh_browser import SshBrowser
from services.desktop.browser.routing.staging import BrowserStaging
from services.desktop.browser.routing.workspace_files import directory_fd, open_parent, publish_at


def test_proof_is_scoped_to_target_purpose_and_lifetime() -> None:
    ids = count()
    now = [0.0]
    authority = BrowserAuthority(new_token=lambda: str(next(ids)), now=lambda: now[0])
    authority.register(71, "one-agent")
    with pytest.raises(ValueError, match="invalid or expired"):
        authority.issue("runner-bearer", "meiva://macmini", PURPOSE)

    proof = authority.issue("one-agent", "meiva://macmini", PURPOSE)["proof"]
    assert isinstance(proof, str)
    with pytest.raises(ValueError, match="bound to another target"):
        authority.redeem(proof, "meiva://other", PURPOSE)
    with pytest.raises(ValueError, match="bound to another target"):
        authority.redeem(proof, "meiva://macmini", "different")
    assert authority.redeem(proof, "meiva://macmini", PURPOSE) == 71
    with pytest.raises(ValueError):
        authority.redeem(proof, "meiva://macmini", PURPOSE)
    late = authority.issue("one-agent", "meiva://macmini", PURPOSE)["proof"]
    assert isinstance(late, str)
    now[0] = 30
    with pytest.raises(ValueError, match="expired"):
        authority.redeem(late, "meiva://macmini", PURPOSE)


async def test_source_identity_is_issued_by_the_supervised_socket(short_tmp: Path) -> None:
    run_dir(short_tmp).mkdir(parents=True)
    sequence = count()
    authority = BrowserAuthority(new_token=lambda: str(next(sequence)), now=lambda: 0.0)
    authority.register(101, "given-to-hosted-agent")
    async with await serve_browser_authority(short_tmp, authority), asyncio.timeout(8.0):
        with serve(home=short_tmp) as root:
            process = root["root"]
            unit = {
                "id": "agent-host",
                "state": "running",
                "pid": process["pid"],
                "create_time": process["create_time"],
                "starttime": process["starttime"],
            }
            root["units"].append(unit)
            response = await asyncio.to_thread(
                hosted_browser_identity,
                short_tmp,
                "issue",
                "given-to-hosted-agent",
                "meiva://peer",
            )
            token = response["proof"]
            assert isinstance(token, str)
            response = await asyncio.to_thread(
                hosted_browser_identity,
                short_tmp,
                "redeem",
                token,
                "meiva://peer",
            )
            assert response["agent_id"] == 101
            unit["pid"] = process["pid"] + 1
            with pytest.raises(ValueError, match="does not belong"):
                await asyncio.to_thread(
                    hosted_browser_identity,
                    short_tmp,
                    "issue",
                    "given-to-hosted-agent",
                    "meiva://peer",
                )


def test_workspace_parent_replacement_cannot_redirect_atomic_write(tmp_path: Path) -> None:
    work = tmp_path / "workspace"
    outside = tmp_path / "outside"
    child = work / "child"
    child.mkdir(parents=True)
    outside.mkdir()
    with directory_fd(work) as root, open_parent(root, ("child",), create=False) as parent:
        _ = child.rename(work / "retained")
        child.symlink_to(outside, target_is_directory=True)
        publish_at(parent, "artifact.txt", b"safe")
    assert (work / "retained" / "artifact.txt").read_bytes() == b"safe"
    assert not (outside / "artifact.txt").exists()
    with pytest.raises(ValueError, match="workspace"):
        CallerFiles(work, "upload_file", {"filePath": "child/artifact.txt"})


def test_workspace_upload_rejects_fifo(tmp_path: Path) -> None:
    os.mkfifo(tmp_path / "pipe")
    with pytest.raises(ValueError, match="regular workspace file"):
        CallerFiles(tmp_path, "upload_file", {"filePath": "pipe"})


async def test_staging_owner_outlives_abandoned_waiter_and_sweeps_residue(tmp_path: Path) -> None:
    stale = tmp_path / "chrome-stale"
    stale.mkdir()
    started = asyncio.Event()
    finish = asyncio.Event()
    paths: list[Path] = []

    async def execute(_tool: str, args: dict[str, object], _agent_id: int) -> CallToolResult:
        path = Path(str(args["filePath"]))
        paths.append(path)
        started.set()
        await finish.wait()
        assert path.read_bytes() == b"payload"
        return CallToolResult(content=[])

    owner = BrowserStaging(tmp_path, execute)
    assert not stale.exists()
    try:
        async with asyncio.TaskGroup() as tasks:
            upload = tasks.create_task(
                owner.run(
                    "upload_file",
                    {"filePath": "x.txt"},
                    {"filePath": "cGF5bG9hZA=="},
                    2,
                )
            )
            await started.wait()
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(asyncio.shield(upload), timeout=0)
            assert paths[0].exists()
            finish.set()
        assert paths[0].exists()
    finally:
        owner.close()
    assert not paths[0].exists()


@pytest.mark.parametrize(
    "name",
    [
        "performance_start_trace",
        "performance_analyze_insight",
        "take_heapsnapshot",
        "lighthouse_audit",
    ],
)
async def test_remote_exports_are_refused_before_invoking_browser(
    tmp_path: Path, name: str
) -> None:
    async def execute(_tool: str, _args: dict[str, object], _agent_id: int) -> CallToolResult:
        pytest.fail("Unsupported remote tool reached Chrome")

    owner = BrowserStaging(tmp_path, execute)
    with pytest.raises(ValueError, match="unavailable on remote"):
        await owner.run(name, {}, {}, 2)


class ChromeFixture:
    def __init__(self, page: int) -> None:
        self.new_page = page
        self.pages: set[int] = {1}
        self.reconnected = False
        self.calls: list[int] = []

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        if name == "new_page":
            self.pages.add(self.new_page)
            text = f"{self.new_page}: https://mine.test/ [selected]"
        elif name == "list_pages":
            text = "\n".join(f"{p}: https://fixture.test/" for p in sorted(self.pages))
        else:
            page = arguments.get("pageId")
            assert isinstance(page, int) and page in self.pages
            self.calls.append(page)
            text = "ok"
        reconnect, self.reconnected = self.reconnected, False
        return CallToolResult(
            content=[TextContent(type="text", text=text)],
            structured_content={"reconnected": True} if reconnect else None,
        )


async def test_ssh_calls_pin_page_ids_and_invalidate_closed_or_reconnected_tabs() -> None:
    config = RemoteHostConfig("sample", "linux", "sample")
    async with asyncio.TaskGroup() as tasks:
        first = SshBrowser(config, tasks=tasks, timers=system_clock(), connect_timeout_s=1)
        second = SshBrowser(config, tasks=tasks, timers=system_clock(), connect_timeout_s=1)
        left, right = ChromeFixture(10), ChromeFixture(20)
        _ = await first._paged_call(left, "new_page", {"url": "https://left.test/"})
        _ = await second._paged_call(right, "new_page", {"url": "https://right.test/"})
        _ = await first._paged_call(left, "take_snapshot", {})
        _ = await second._paged_call(right, "take_snapshot", {})
        left.pages.remove(10)
        with pytest.raises(ValueError, match="No owned"):
            await first._paged_call(left, "click", {"uid": "1"})
        right.reconnected = True
        with pytest.raises(ValueError, match="reconnected"):
            await second._paged_call(right, "click", {"uid": "1"})
        with pytest.raises(ValueError, match="No owned"):
            await second._paged_call(right, "click", {"uid": "1"})
        assert left.calls == [10]
        assert right.calls == [20]
