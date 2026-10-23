from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

from base.deploy.lifecycle.start_serving import RootBirth


@pytest.fixture
def state_path(tmp_path: Path) -> Path:
    from base.deploy.lifecycle import start_serving

    return start_serving.state_path(tmp_path)


def test_new_start_attempt_invalidates_a_previous_serving_generation(
    serving_root: RootBirth, state_path: Path, tmp_path: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    previous = start_serving.begin_start(home=tmp_path)
    assert start_serving.mark_serving(previous, runtime=serving_root.runtime, home=tmp_path) is True
    assert start_serving.is_serving(home=tmp_path) is True

    current = start_serving.begin_start(home=tmp_path)

    assert start_serving.is_serving(home=tmp_path) is False
    assert (
        start_serving.mark_serving(previous, runtime=serving_root.runtime, home=tmp_path) is False
    )
    assert start_serving.mark_serving(current, runtime=serving_root.runtime, home=tmp_path) is True
    assert start_serving.is_serving(home=tmp_path) is True


def test_clear_serving_keeps_revival_blocked(
    serving_root: RootBirth, state_path: Path, tmp_path: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    generation = start_serving.begin_start(home=tmp_path)
    assert (
        start_serving.mark_serving(generation, runtime=serving_root.runtime, home=tmp_path) is True
    )

    start_serving.clear_serving(home=tmp_path)

    assert start_serving.is_serving(home=tmp_path) is False


def test_start_waits_for_an_authorized_recovery_action(
    serving_root: RootBirth, state_path: Path, tmp_path: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    generation = start_serving.begin_start(home=tmp_path)
    assert (
        start_serving.mark_serving(generation, runtime=serving_root.runtime, home=tmp_path) is True
    )
    recovery_authorized = Event()
    release_recovery = Event()
    start_completed = Event()

    def recover() -> None:
        with start_serving.recovery_permitted(home=tmp_path) as permitted:
            assert permitted is True
            recovery_authorized.set()
            assert release_recovery.wait(timeout=2)

    def start_again() -> None:
        start_serving.begin_start(home=tmp_path)
        start_completed.set()

    recovery_thread = Thread(target=recover)
    recovery_thread.start()
    assert recovery_authorized.wait(timeout=2)
    start_thread = Thread(target=start_again)
    start_thread.start()

    assert start_completed.wait(timeout=0.1) is False
    release_recovery.set()
    recovery_thread.join(timeout=2)
    start_thread.join(timeout=2)
    assert start_completed.is_set()
    assert start_serving.is_serving(home=tmp_path) is False


@pytest.mark.parametrize("change", ["root", "launch", "runtime", "home", "unknown"])
def test_old_birth_cannot_grant_after_observed_generation_changes(
    serving_root: RootBirth, state_path: Path, tmp_path: Path, change: str
) -> None:
    from base.deploy.lifecycle import start_serving
    from base.native_process.root_control.client import RootClientError

    generation = start_serving.begin_start(home=tmp_path)
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime, home=tmp_path)
    original = state_path.read_bytes()
    observed = serving_root
    if change == "root":
        process = serving_root.process.model_copy(update={"starttime": 457})
        observed = serving_root.model_copy(update={"process": process})
    elif change == "launch":
        observed = serving_root.model_copy(update={"launch_digest": "c" * 64})
    elif change == "runtime":
        runtime = serving_root.runtime.model_copy(update={"source_digest": "c" * 64})
        observed = serving_root.model_copy(update={"runtime": runtime})
    elif change == "home":
        observed = serving_root.model_copy(update={"home": "/another/home"})

    def current() -> RootBirth:
        if change == "unknown":
            raise RootClientError("native peer unreadable")
        return observed

    assert start_serving.born_identity(home=tmp_path, observe_root_fn=current) is None
    assert not start_serving.is_serving(home=tmp_path, observe_root_fn=current)
    with start_serving.recovery_permitted(home=tmp_path, observe_root_fn=current) as permitted:
        assert not permitted
    assert state_path.read_bytes() == original


def test_marker_rejects_a_different_loaded_runtime(
    serving_root: RootBirth, state_path: Path, tmp_path: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    generation = start_serving.begin_start(home=tmp_path)
    other = serving_root.runtime.model_copy(update={"interpreter": "/foreign/python"})
    with pytest.raises(RuntimeError, match="admitted loaded runtime"):
        start_serving.mark_serving(generation, runtime=other, home=tmp_path)
    assert not start_serving.is_serving(home=tmp_path)


@pytest.mark.parametrize(
    "contents", ["{}", '{"schema_version":1,"state":"serving","generation":"old"}']
)
def test_missing_or_old_markers_are_not_birth_evidence(
    state_path: Path, tmp_path: Path, contents: str
) -> None:
    from base.deploy.lifecycle import start_serving

    assert start_serving.born_identity(home=tmp_path) is None
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(contents)
    assert start_serving.born_identity(home=tmp_path) is None


def test_linux_birth_uses_ticks_not_recomputed_wall_clock(serving_root: RootBirth) -> None:
    shifted = serving_root.process.model_copy(update={"create_time": 999.0})
    assert serving_root.same_generation(serving_root.model_copy(update={"process": shifted}))
    missing = serving_root.process.model_copy(update={"starttime": None})
    assert not serving_root.same_generation(serving_root.model_copy(update={"process": missing}))


def test_birth_read_refuses_marker_replaced_during_native_observation(
    serving_root: RootBirth, state_path: Path, tmp_path: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    generation = start_serving.begin_start(home=tmp_path)
    assert start_serving.mark_serving(generation, runtime=serving_root.runtime, home=tmp_path)

    def raced() -> RootBirth:
        start_serving.begin_start(home=tmp_path)
        return serving_root

    assert start_serving.born_identity(home=tmp_path, observe_root_fn=raced) is None


def test_direct_process_e2e_injects_only_its_explicit_fixture_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from base.deploy.lifecycle import start_serving
    from base.deploy.lifecycle.tests.serving_entrypoint import inject_fixture_gate

    gate = tmp_path / "e2e-serving"
    monkeypatch.setattr(start_serving, "is_serving", start_serving.is_serving)
    monkeypatch.setattr(start_serving, "recovery_permitted", start_serving.recovery_permitted)

    inject_fixture_gate(gate)

    assert not start_serving.is_serving()
    gate.write_text("ready\n")
    assert start_serving.is_serving()
    with start_serving.recovery_permitted() as permitted:
        assert permitted
    gate.unlink()
    assert not start_serving.is_serving()
