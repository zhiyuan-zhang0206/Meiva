from __future__ import annotations

import asyncio
import signal
from pathlib import Path

import pytest

from services.agent_runner.agent_host import daemon


def _make_plugin(root: Path, name: str, body: str = "x = 1\n") -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "plugin.py").write_text(body, encoding="utf-8")


def test_fingerprint_empty_when_no_plugins_dir(tmp_path: Path) -> None:
    assert daemon.plugins_fingerprint(tmp_path / "nope") == ""


def test_fingerprint_tracks_plugin_add_change_remove(tmp_path: Path) -> None:
    assert daemon.plugins_fingerprint(tmp_path) == ""
    _make_plugin(tmp_path, "alpha")
    f1 = daemon.plugins_fingerprint(tmp_path)
    assert "alpha" in f1

    _make_plugin(tmp_path, "beta")
    f2 = daemon.plugins_fingerprint(tmp_path)
    assert f1 != f2 and "beta" in f2

    (tmp_path / "notes.txt").write_text("hi", encoding="utf-8")
    assert daemon.plugins_fingerprint(tmp_path) == f2

    _make_plugin(tmp_path, "alpha", body="x = 2\n")
    f3 = daemon.plugins_fingerprint(tmp_path)
    assert f3 != f2

    import shutil as _shutil

    _shutil.rmtree(tmp_path / "beta")
    f4 = daemon.plugins_fingerprint(tmp_path)
    assert f4 != f3 and "beta" not in f4


@pytest.mark.asyncio
async def test_watch_restarts_on_plugin_change(tmp_path: Path) -> None:
    raised: list[int] = []
    # meiva:note
    # The watcher raises SIGTERM at its own process; a real handler observes it.
    previous = signal.signal(signal.SIGTERM, lambda signum, _frame: raised.append(signum))
    try:
        watcher = asyncio.create_task(daemon.watch_plugins_for_restart(tmp_path, interval_s=0.01))
        await asyncio.sleep(0.05)
        assert raised == []

        _make_plugin(tmp_path, "alpha")
        await asyncio.wait_for(watcher, 5)
    finally:
        signal.signal(signal.SIGTERM, previous)

    assert raised == [signal.SIGTERM]
