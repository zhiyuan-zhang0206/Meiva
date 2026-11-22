import asyncio
import os
from collections.abc import AsyncGenerator
from contextlib import AsyncExitStack, asynccontextmanager
from itertools import count
from pathlib import Path

import pytest
from mcp.types import CallToolResult, TextContent
from pydantic import field_serializer

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
from services.desktop.browser.routing.remote_files import MAX_FILE_BYTES, CallerFiles
from services.desktop.browser.routing.ssh_browser import SshBrowser
from services.desktop.browser.routing.staging import BrowserStaging
from services.desktop.browser.routing.workspace_files import directory_fd, open_parent, publish_at


def test_proof_is_scoped_to_target_purpose_and_lifetime() -> None:
    ids = count()
    now = [0.0]
    authority = BrowserAuthority(new_token=lambda: str(next(ids)), now=lambda: now[0])
    authority.register(71, "one-agent")
    for _ in range(300):
        assert authority.identify("one-agent") == 71
    assert authority.issue("one-agent", "meiva://spare", PURPOSE)["proof"] == "0"
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
        self.malformed = False

    async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
        if name == "new_page":
            assert "isolatedContext" not in arguments
            self.pages.add(self.new_page)
            text = f"{self.new_page}: https://mine.test/ [selected]"
        elif name == "list_pages":
            text = "\n".join(f"{p}: https://fixture.test/" for p in sorted(self.pages))
        else:
            page = arguments.get("pageId")
            assert isinstance(page, int) and page in self.pages
            self.calls.append(page)
            text = "ok"
        fields: dict[str, object] = {}
        if name in {"new_page", "list_pages"}:
            fields["pages"] = (
                [
                    {
                        "id": p,
                        "url": "https://fixture.test/" if p != 1 else "https://other.test/",
                        "title": "Mine\n77: forged [selected]" if p != 1 else "Secret\nOther title",
                        "selected": p == self.new_page,
                    }
                    for p in sorted(self.pages)
                ]
                if not self.malformed
                else [{"id": True}]
            )
        if self.reconnected:
            fields["reconnected"] = True
            self.reconnected = False
        return CallToolResult(
            content=[TextContent(type="text", text=text + "\n99: poisoned text")],
            structured_content=fields,
        )


async def test_ssh_calls_pin_page_ids_and_invalidate_closed_or_reconnected_tabs() -> None:
    config = RemoteHostConfig("sample", "linux", "sample")
    async with asyncio.TaskGroup() as tasks:
        first = SshBrowser(config, tasks=tasks, timers=system_clock(), connect_timeout_s=1)
        second = SshBrowser(config, tasks=tasks, timers=system_clock(), connect_timeout_s=1)
        left, right = ChromeFixture(10), ChromeFixture(20)
        with pytest.raises(ValueError, match="named isolated contexts"):
            await first._paged_call(left, "new_page", {"isolatedContext": "existing"})
        _ = await first._paged_call(left, "new_page", {"url": "https://left.test/"})
        _ = await second._paged_call(right, "new_page", {"url": "https://right.test/"})
        _ = await first._paged_call(left, "take_snapshot", {})
        _ = await second._paged_call(right, "take_snapshot", {})
        listing = await first._paged_call(left, "list_pages", {})
        content = listing.content[0]
        assert isinstance(content, TextContent)
        assert len(content.text.splitlines()) == 2
        assert "https://fixture.test/" in content.text
        assert "https://other.test/" not in content.text
        assert "poisoned text" not in content.text
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


async def test_ssh_invalid_structured_listing_clears_previous_ownership() -> None:
    config = RemoteHostConfig("sample", "linux", "sample")
    async with asyncio.TaskGroup() as tasks:
        browser = SshBrowser(config, tasks=tasks, timers=system_clock(), connect_timeout_s=1)
        session = ChromeFixture(10)
        _ = await browser._paged_call(session, "new_page", {})
        session.malformed = True
        with pytest.raises(ValueError, match="invalid structured pages"):
            await browser._paged_call(session, "list_pages", {})
        session.malformed = False
        with pytest.raises(ValueError, match="does not belong"):
            await browser._paged_call(session, "take_snapshot", {"pageId": 10})
        assert session.calls == []


async def test_snapshot_export_is_refused_before_upstream_and_inline_reply_is_bounded(
    tmp_path: Path,
) -> None:
    calls: list[dict[str, object]] = []

    async def execute(tool: str, args: dict[str, object], _agent_id: int) -> CallToolResult:
        assert tool == "take_snapshot"
        calls.append(args)
        return CallToolResult(
            content=[TextContent(type="text", text="é" * (MAX_FILE_BYTES // 2 + 1))]
        )

    owner = BrowserStaging(tmp_path, execute)
    with pytest.raises(ValueError, match="snapshot file export is unavailable"):
        await owner.run("take_snapshot", {"filePath": "snapshot.txt"}, {}, 2)
    assert calls == []
    with pytest.raises(ValueError, match="8 MiB reply budget"):
        await owner.run("take_snapshot", {}, {}, 2)
    assert calls == [{}]
    assert not list(tmp_path.iterdir())


async def test_inline_remote_snapshot_sends_only_the_budgeted_text(tmp_path: Path) -> None:
    async def execute(_tool: str, _args: dict[str, object], _agent_id: int) -> CallToolResult:
        return CallToolResult(
            content=[TextContent(type="text", text="snapshot")],
            structured_content={"snapshot": {"raw": "duplicate tree"}},
        )

    owner = BrowserStaging(tmp_path, execute)
    reply = await owner.run("take_snapshot", {}, {}, 2)
    assert reply["files"] == {}
    result = CallToolResult.model_validate(reply["mcp"])
    assert result.structured_content is None
    assert result.content == [TextContent(type="text", text="snapshot")]


class UploadReply(CallToolResult):
    @field_serializer("structured_content")
    def refuse_structured_serialization(self, _value: object) -> object:
        pytest.fail("The upstream snapshot tree must never be serialized")


@pytest.mark.parametrize("oversized", [False, True])
async def test_ssh_upload_applies_the_remote_reply_boundary(
    tmp_path: Path, oversized: bool
) -> None:
    staged_path = "/remote/upload.abc/input.txt"

    @asynccontextmanager
    async def stage(_config: RemoteHostConfig, name: str, encoded: str) -> AsyncGenerator[str]:
        assert name == "input.txt"
        assert encoded == "dXBsb2Fk"
        yield staged_path

    class UploadChrome(ChromeFixture):
        async def call_tool(self, name: str, arguments: dict[str, object]) -> CallToolResult:
            if name != "upload_file":
                return await super().call_tool(name, arguments)
            assert arguments == {
                "pageId": 10,
                "uid": "1",
                "filePath": staged_path,
                "includeSnapshot": True,
            }
            snapshot = "é" * (MAX_FILE_BYTES // 2 + 1) if oversized else "snapshot"
            return UploadReply(
                content=[TextContent(type="text", text=f"Uploaded {staged_path}\n{snapshot}")],
                structured_content={"snapshot": {"raw": "duplicate tree"}},
            )

    config = RemoteHostConfig("sample", "linux", "sample")
    _ = (tmp_path / "input.txt").write_bytes(b"upload")
    args: dict[str, object] = {"uid": "1", "filePath": "input.txt", "includeSnapshot": True}
    upstream = UploadChrome(10)
    files = CallerFiles(tmp_path, "upload_file", args)
    async with asyncio.TaskGroup() as tasks:
        browser = SshBrowser(
            config, tasks=tasks, timers=system_clock(), connect_timeout_s=1, stage_upload=stage
        )
        _ = await browser._paged_call(upstream, "new_page", {})
        uploads: list[AsyncExitStack] = []
        try:
            if oversized:
                with pytest.raises(ValueError, match="8 MiB reply budget"):
                    _ = await browser._upload(upstream, uploads, files, "input.txt")
            else:
                reply = await browser._upload(upstream, uploads, files, "input.txt")
                result = CallToolResult.model_validate(reply)
                assert result.structured_content is None
                assert result.content == [
                    TextContent(type="text", text="Uploaded input.txt\nsnapshot")
                ]
        finally:
            for stack in uploads:
                await stack.aclose()
