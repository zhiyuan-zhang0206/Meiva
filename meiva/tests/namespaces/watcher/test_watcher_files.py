from __future__ import annotations

import pathlib

import pytest

import meiva
from meiva import watcher
from meiva.watcher import wiring

pytestmark = [
    pytest.mark.usefixtures("pty_service", "_isolated_agent"),
]

_SLEEPER = "import time\ntime.sleep(60)\n"


def _watchers_dir() -> pathlib.Path:
    return wiring.spawn_deps().watchers_dir


def _write_pair(directory: pathlib.Path, session_id: int, text: str) -> list[pathlib.Path]:
    paths = [
        directory / f"watcher_{session_id}.py",
        directory / f"watcher_{session_id}_boot.py",
        directory / f"watcher_{session_id}_launch.sh",
    ]
    for path in paths:
        path.write_text(text)
    return paths


def test_watcher_script_dir_is_tmp_per_agent(_agent_row: int) -> None:
    import tempfile

    from base.paths import meiva_home

    d = _watchers_dir()
    td = pathlib.Path(tempfile.gettempdir())
    assert d.is_relative_to(td / "meiva")
    assert str(meiva_home()) not in str(d)
    assert d.name == "watchers"
    slug = meiva_home().name.lstrip(".") or "cluster"
    assert any(part == slug or part.startswith(f"{slug}-") for part in d.parts)
    assert d.is_dir()


def test_spawn_prunes_stale_watcher_files(_agent_row: int) -> None:
    d = _watchers_dir()
    stale = _write_pair(d, 999, "old")
    keep = d / "keep_me.py"
    keep.write_text("not generated")

    handle = watcher.launch(_SLEEPER, timeout="1h", name="test-prune")
    try:
        wid = handle.session_id
        assert not any(path.exists() for path in stale)
        assert keep.exists()
        assert (d / f"watcher_{wid}.py").exists()
        assert (d / f"watcher_{wid}_boot.py").exists()
    finally:
        meiva.shell.sessions.kill(handle.session_id)


def test_prune_does_not_touch_other_agents_files(_agent_row: int) -> None:
    d = _watchers_dir()
    other = d.parent / str(meiva.self.AGENT_ID + 1) / "watchers"
    other.mkdir(parents=True, exist_ok=True)
    foreign = other / "watcher_999.py"
    foreign.write_text("someone else's")

    handle = watcher.launch(_SLEEPER, timeout="1h", name="test-isolation")
    try:
        assert foreign.exists()
    finally:
        meiva.shell.sessions.kill(handle.session_id)


def test_spawn_keeps_sibling_files_while_session_alive(_agent_row: int) -> None:
    d = _watchers_dir()
    live_session = meiva.shell.sessions.new("alive-sibling", ttl=300)
    live = _write_pair(d, live_session, "still needed")
    dead = _write_pair(d, live_session + 1000, "dead")

    handle = watcher.launch(_SLEEPER, timeout="1h", name="test-prune-live")
    try:
        assert all(path.exists() for path in live)
        assert not any(path.exists() for path in dead)
        assert (d / f"watcher_{handle.session_id}.py").exists()
    finally:
        meiva.shell.sessions.kill(handle.session_id)
        meiva.shell.sessions.kill(live_session)


def test_spawn_back_to_back_keeps_all_files(_agent_row: int) -> None:
    d = _watchers_dir()
    handles = [
        watcher.launch(_SLEEPER, timeout="1h", name=name) for name in ("b2b-1", "b2b-2", "b2b-3")
    ]
    try:
        ids = [h.session_id for h in handles]
        assert len(set(ids)) == 3
        for wid in ids:
            assert (d / f"watcher_{wid}.py").exists(), f"watcher_{wid}.py pruned"
            assert (d / f"watcher_{wid}_boot.py").exists(), f"watcher_{wid}_boot.py pruned"
    finally:
        for h in handles:
            meiva.shell.sessions.kill(h.session_id)
