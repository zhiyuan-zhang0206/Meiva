"""Shared root-observation fixture owned by the serving lifecycle package."""

from collections.abc import Callable
from pathlib import Path

import pytest

from base.deploy.lifecycle.start_serving import RootBirth

_override: list[Callable[[Path], RootBirth]] = []
_observed: list[RootBirth] = []


def override_observed_root(fn: Callable[[Path], RootBirth]) -> None:
    _override[:] = [fn]


# meiva:note
# The observation a reader of this unit is handed in place of `observe_root`: the root this
# test's `serving_root` marks, or the override a case installed. A reader takes it as its
# `observe` argument; the marker, its lock and the generation comparisons stay real.
def observe(home: Path) -> RootBirth:
    if _override:
        return _override[0](home)
    if not _observed:
        raise LookupError("request the serving_root fixture before observing this unit's root")
    return _observed[0]


# meiva:note
# An observation that cannot answer: the root peer is unreachable, so a marked birth can never be
# confirmed against it. A reader handed this one must report the unit as not serving.
def observe_unreadable(home: Path) -> RootBirth:
    from base.native_process.root_control.client import RootClientError

    raise RootClientError(f"root supervisor unreachable at {home}")


@pytest.fixture
def serving_root(tmp_path: Path) -> RootBirth:
    """Explicit local-root observation double; excludes native/root proof.

    Maintenance/readiness unit tests retain the actual marker and locking code.
    Native IPC and loaded-origin contracts have independent real socket tests.
    """
    from base.deploy.lifecycle import start_serving
    from base.host.runtime_interpreter import LoadedRuntimeIdentity
    from base.native_process.evidence import ExpectedProcess

    runtime = LoadedRuntimeIdentity(
        kind="source",
        code_root=str(tmp_path),
        interpreter=str(tmp_path / "python"),
        prefix=str(tmp_path / "venv"),
        cwd=str(tmp_path),
        source_digest="a" * 64,
    )
    birth = start_serving.RootBirth(
        home=str(tmp_path),
        process=ExpectedProcess(pid=4321, create_time=123.0, starttime=456),
        launch_digest="b" * 64,
        runtime=runtime,
    )
    _override.clear()
    _observed[:] = [birth]
    return birth
