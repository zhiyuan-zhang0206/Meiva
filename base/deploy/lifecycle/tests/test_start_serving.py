from __future__ import annotations

from pathlib import Path
from threading import Event, Thread

import pytest

from base.deploy.lifecycle.start_serving import RootBirth
from base.deploy.lifecycle.tests.serving_root import observe, override_observed_root
from base.host.tests.short_tmp import short_tmp as short_tmp
from base.paths import root_run_dir


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture
def state_path(home: Path) -> Path:
    from base.deploy.lifecycle import start_serving

    return start_serving.state_path(home)


def test_new_start_attempt_invalidates_a_previous_serving_generation(
    serving_root: RootBirth, state_path: Path, home: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    previous = start_serving.begin_start(home)
    assert (
        start_serving.mark_serving(home, previous, runtime=serving_root.runtime, observe=observe)
        is True
    )
    assert start_serving.is_serving(home, observe=observe) is True

    current = start_serving.begin_start(home)

    assert start_serving.is_serving(home, observe=observe) is False
    assert (
        start_serving.mark_serving(home, previous, runtime=serving_root.runtime, observe=observe)
        is False
    )
    assert (
        start_serving.mark_serving(home, current, runtime=serving_root.runtime, observe=observe)
        is True
    )
    assert start_serving.is_serving(home, observe=observe) is True


def test_clear_serving_keeps_revival_blocked(
    serving_root: RootBirth, state_path: Path, home: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    generation = start_serving.begin_start(home)
    assert (
        start_serving.mark_serving(home, generation, runtime=serving_root.runtime, observe=observe)
        is True
    )

    start_serving.clear_serving(home)

    assert start_serving.is_serving(home, observe=observe) is False
    # meiva:note
    # Blocked revival is the reader that grants a revival action, asserted on its own: a cleared
    # home answers `is_serving` false and must refuse recovery, not just fail to look alive.
    with start_serving.recovery_permitted(home, observe=observe) as permitted:
        assert permitted is False


def test_start_waits_for_an_authorized_recovery_action(
    serving_root: RootBirth, state_path: Path, home: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    generation = start_serving.begin_start(home)
    assert (
        start_serving.mark_serving(home, generation, runtime=serving_root.runtime, observe=observe)
        is True
    )
    recovery_authorized = Event()
    release_recovery = Event()
    start_completed = Event()

    def recover() -> None:
        with start_serving.recovery_permitted(home, observe=observe) as permitted:
            assert permitted is True
            recovery_authorized.set()
            assert release_recovery.wait(timeout=2)

    def start_again() -> None:
        start_serving.begin_start(home)
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
    assert start_serving.is_serving(home, observe=observe) is False


@pytest.mark.parametrize("change", ["root", "launch", "runtime", "home", "unknown"])
def test_old_birth_cannot_grant_after_observed_generation_changes(
    serving_root: RootBirth,
    state_path: Path,
    home: Path,
    change: str,
) -> None:
    from base.deploy.lifecycle import start_serving
    from base.native_process.root_control.client import RootClientError

    generation = start_serving.begin_start(home)
    assert start_serving.mark_serving(
        home, generation, runtime=serving_root.runtime, observe=observe
    )
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

    def current(_home: Path) -> RootBirth:
        if change == "unknown":
            raise RootClientError("native peer unreadable")
        return observed

    override_observed_root(current)
    assert start_serving.born_identity(home, observe=observe) is None
    assert not start_serving.is_serving(home, observe=observe)
    with start_serving.recovery_permitted(home, observe=observe) as permitted:
        assert not permitted
    assert state_path.read_bytes() == original


def test_marker_rejects_a_different_loaded_runtime(
    serving_root: RootBirth, state_path: Path, home: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    generation = start_serving.begin_start(home)
    other = serving_root.runtime.model_copy(update={"interpreter": "/foreign/python"})
    with pytest.raises(RuntimeError, match="admitted loaded runtime"):
        start_serving.mark_serving(home, generation, runtime=other, observe=observe)
    assert not start_serving.is_serving(home, observe=observe)


@pytest.mark.parametrize(
    "contents", ["{}", '{"schema_version":1,"state":"serving","generation":"old"}']
)
def test_missing_or_old_markers_are_not_birth_evidence(
    state_path: Path, home: Path, contents: str
) -> None:
    from base.deploy.lifecycle import start_serving

    assert start_serving.born_identity(home) is None
    state_path.parent.mkdir(parents=True)
    state_path.write_text(contents)
    assert start_serving.born_identity(home) is None


def test_linux_birth_uses_ticks_not_recomputed_wall_clock(serving_root: RootBirth) -> None:
    shifted = serving_root.process.model_copy(update={"create_time": 999.0})
    assert serving_root.same_generation(serving_root.model_copy(update={"process": shifted}))
    missing = serving_root.process.model_copy(update={"starttime": None})
    assert not serving_root.same_generation(serving_root.model_copy(update={"process": missing}))


def test_birth_read_refuses_marker_replaced_during_native_observation(
    serving_root: RootBirth, state_path: Path, home: Path
) -> None:
    from base.deploy.lifecycle import start_serving

    generation = start_serving.begin_start(home)
    assert start_serving.mark_serving(
        home, generation, runtime=serving_root.runtime, observe=observe
    )

    def raced(_home: Path) -> RootBirth:
        start_serving.begin_start(home)
        return serving_root

    override_observed_root(raced)
    assert start_serving.born_identity(home, observe=observe) is None


# meiva:note
# The end-to-end path the e2e suite and the direct-process fixture rely on: a socket served by a
# process, the receipt that names it, and the reads that confirm it -- then a stop clears it.
def test_a_socket_marked_birth_serves_until_a_stop_clears_it(short_tmp: Path) -> None:
    from base.deploy.lifecycle import start_serving
    from base.native_process.root_control.tests.status_root import (
        birth_from_response,
        serve,
        write_receipt,
    )

    home = short_tmp / "home"
    home.mkdir()
    with serve(home=home) as answer:
        write_receipt(home, birth_from_response(answer["root"]))

        assert start_serving.is_serving(home) is True
        with start_serving.recovery_permitted(home) as permitted:
            assert permitted

        start_serving.clear_serving(home)

        assert start_serving.is_serving(home) is False
        with start_serving.recovery_permitted(home) as permitted:
            assert permitted is False

    # meiva:note
    # The socket and the marker are the home's only residue; both leave when the server stops.
    assert not (root_run_dir(home) / "meiva-root.sock").exists()
    assert not start_serving.state_path(home).exists()
