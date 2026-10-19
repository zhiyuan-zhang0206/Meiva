from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

from base.lm.harness.antigravity.session import AgySession
from base.lm.harness.antigravity.tests.support import Harness, start


class _SilentSession:
    async def events(self) -> AsyncIterator[Any]:
        if False:
            yield None

    async def close(self, *, reason: str = "") -> None:
        return None


def _harness(tmp_path: Path) -> Harness:
    session = cast(AgySession, _SilentSession())
    return Harness(
        session, tmp_path / "received.jsonl", tmp_path / "argv.jsonl", tmp_path / "child.jsonl"
    )


async def test_the_argv_reader_returns_nothing_while_the_record_has_not_landed(
    tmp_path: Path,
) -> None:
    # meiva:note
    # `argv_seen()` is the predicate the tests poll every 5ms, so it runs against the
    # file at every instant of the write: absent, created but empty, then complete.
    # The middle state used to raise IndexError on `splitlines()[0]`; the Trunk merge
    # tree hit it for #6393.
    harness = _harness(tmp_path)
    try:
        assert harness.argv_seen() == []
        harness.argv.write_bytes(b"")
        assert harness.argv_seen() == []
        harness.argv.write_text(json.dumps(["--flag"]) + "\n", encoding="utf-8")
        assert harness.argv_seen() == ["--flag"]
    finally:
        await harness.stop()


async def test_polling_the_record_while_the_fake_cli_starts_never_reads_a_partial_line(
    tmp_path: Path,
) -> None:
    harness = await start(tmp_path)
    try:
        snapshots: list[list[str]] = []
        for _ in range(400):
            snapshots.append(harness.argv_seen())
            if snapshots[-1]:
                break
            await asyncio.sleep(0.001)
        assert snapshots[-1] != [], "the fake CLI never recorded its argv"
        assert all(seen == [] for seen in snapshots[:-1]), "a poll observed a half-written record"
    finally:
        await harness.stop()
