from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from base.clock.tests.fakes import system_clock
from base.lm.harness.antigravity.config import AntigravityConfig
from base.lm.harness.antigravity.transport import AgyTransport

FAKE_CLI = Path(__file__).with_name("fake_agy.py")


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
    transport = AgyTransport(config, clock=system_clock())
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
    transport = AgyTransport(config, clock=system_clock())
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
    transport = AgyTransport(
        config,
        clock=system_clock(),
        max_frame_bytes=4096 if overflow else 12 * 1024**2,
    )
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
