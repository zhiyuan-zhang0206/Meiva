from __future__ import annotations

import datetime
import pathlib
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import meiva
from meiva import watcher
from meiva.shell import SessionHandle
from meiva.watcher import plans, spawn, wiring

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


# meiva:note
# Bug A (#1116), the interleaving that the order of `spawn.run` exists for: the deps of one launch
# were built before a sibling watcher existed, then the sibling got its session and wrote its
# files, then the launch ran. The sibling's child has not read its files yet, so they must stay.
# A live-session snapshot carried in the deps would not contain the sibling and the launch would
# delete its files; `run` lists the sessions itself, after it created its own.
def test_a_sibling_that_starts_after_the_deps_were_built_keeps_its_unread_files(
    _agent_row: int,
) -> None:
    deps = wiring.spawn_deps()
    sibling = meiva.shell.sessions.new("late-sibling", ttl=300)
    unread = _write_pair(deps.watchers_dir, sibling, "not read by the sibling's child yet")
    request = plans.plan_launch(
        _SLEEPER, "1h", "test-late-sibling", None, datetime.datetime.now(datetime.UTC)
    )

    handle = spawn.run(request, deps)
    try:
        assert all(path.exists() for path in unread)
    finally:
        meiva.shell.sessions.kill(handle.session_id)
        meiva.shell.sessions.kill(sibling)


# meiva:note
# Watchers launched at the same moment on real threads: each one creates its session, lists the
# directory, lists the sessions and removes what is dead, while the others do the same. None may
# delete a file whose watcher was created by a sibling in the meantime.
def test_watchers_launched_at_the_same_moment_keep_each_others_files(_agent_row: int) -> None:
    directory = wiring.spawn_deps().watchers_dir
    names = ("sib-a", "sib-b", "sib-c")
    launched: list[SessionHandle] = []
    try:
        for _round in range(3):
            gate = Barrier(len(names))

            def start(name: str, gate: Barrier = gate) -> SessionHandle:
                gate.wait()
                return watcher.launch(_SLEEPER, timeout="1h", name=name)

            with ThreadPoolExecutor(max_workers=len(names)) as pool:
                handles = list(pool.map(start, names))
            launched.extend(handles)
            for handle in handles:
                assert (directory / f"watcher_{handle.session_id}.py").exists()
                assert (directory / f"watcher_{handle.session_id}_boot.py").exists()
    finally:
        for handle in launched:
            meiva.shell.sessions.kill(handle.session_id)
