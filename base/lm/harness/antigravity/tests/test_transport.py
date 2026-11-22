from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from base.clock.tests.fakes import system_clock
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.transport import MAX_FRAME_BYTES, AgyTransport
from base.lm.harness.process import PsutilChildProbe

FAKE_CLI = Path(__file__).with_name("fake_agy.py")


def _transport(
    config: AntigravityConfig, *, max_frame_bytes: int = MAX_FRAME_BYTES
) -> AgyTransport:
    return AgyTransport(
        config,
        environ={"HOME": str(config.cwd)},
        owner_uid=os.getuid(),
        generation_id=uuid4().hex,
        clock=system_clock(),
        max_frame_bytes=max_frame_bytes,
    )


def test_argv_selects_acp_and_preserves_permission_policy(tmp_path: Path) -> None:
    assert AntigravityConfig(cwd=tmp_path).command() == ("agy", "acp")
    config = AntigravityConfig(
        cwd=tmp_path,
        executable=("fake",),
        skip_permissions=True,
        extra_args=("--stdio",),
    )
    assert config.command() == ("fake", "acp", "--dangerously-skip-permissions", "--stdio")


async def test_startup_rejects_earlier_cli_that_preempts_steer(tmp_path: Path) -> None:
    config = AntigravityConfig(
        cwd=tmp_path,
        executable=(sys.executable, str(FAKE_CLI)),
        extra_args=("--test-legacy",),
    )
    transport = _transport(config)
    await transport.start()
    try:
        with pytest.raises(RuntimeError, match=r"lossless ACP steering requires agy 1\.5\.14"):
            _ = await transport.open_session(resume=None)
    finally:
        await transport.close()


async def test_cli_that_cannot_start_acp_reports_version_exit_and_stderr(tmp_path: Path) -> None:
    config = AntigravityConfig(
        cwd=tmp_path,
        executable=(sys.executable, str(FAKE_CLI)),
        extra_args=("--test-no-acp",),
    )
    transport = _transport(config)
    await transport.start()
    try:
        with pytest.raises(
            RuntimeError, match=r"(?s)requires agy 1\.5\.14.*exit=2.*unknown command acp"
        ):
            _ = await transport.open_session(resume=None)
    finally:
        await transport.close()


@pytest.mark.parametrize("overflow", [False, True])
async def test_stdout_assembles_large_and_unterminated_frames(
    tmp_path: Path, overflow: bool
) -> None:
    config = AntigravityConfig(
        cwd=tmp_path,
        executable=(sys.executable, str(FAKE_CLI)),
        extra_args=("--test-large-frames",),
    )
    transport = _transport(config, max_frame_bytes=4096 if overflow else 12 * 1024**2)
    await transport.start()
    try:
        assert await transport.open_session(resume=None) == "conv-frames"
        async with asyncio.timeout(5):
            frames = [frame async for frame in transport.events()]
        if overflow:
            assert frames == []
            assert any("stream line exceeded" in line for line in transport.stderr_tail)
        else:
            assert frames == [
                {"jsonrpc": "2.0", "blob": "x" * (10 * 1024**2)},
                {"jsonrpc": "2.0", "tail": "end"},
            ]
    finally:
        await transport.close()


async def test_close_recovers_an_unsampled_child_that_created_its_own_session(
    tmp_path: Path,
) -> None:
    config = AntigravityConfig(
        cwd=tmp_path,
        executable=(sys.executable, str(FAKE_CLI)),
        extra_args=("--test-child",),
        cancel_settle_seconds=0.5,
    )
    transport = _transport(config)
    await transport.start()
    try:
        assert await transport.open_session(resume=None) == "conv-acp"
        transport.prompt(
            transport.next_request_id(),
            [{"type": "text", "text": "marker\neof"}],
            before_write=lambda: None,
        )
        async with asyncio.timeout(3):
            while transport.returncode is None:
                await asyncio.sleep(0.005)
        assert transport.returncode == 0
        pid = int((tmp_path / "child.pid").read_text())
        probe = PsutilChildProbe()
        assert probe.is_alive(pid)
        await transport.close(broken=True)
        assert not probe.is_alive(pid)
    finally:
        await transport.close(broken=True)
