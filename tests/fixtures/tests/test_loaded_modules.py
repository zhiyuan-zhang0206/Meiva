# meiva:note
# The CI failure this pins: a telemetry quiesce looked a sink up with
# `sys.modules.get` and called the attribute straight away, so a worker that was
# importing that module on another thread handed back the half-built module and the
# fixture died with "partially initialized module ... has no attribute". The race is
# not reproducible on demand, so the half-built state is installed directly: a module
# in `sys.modules` whose spec is still initializing. A module that finished importing
# and lost the attribute must stay an error — that is a rename or a deletion, not a
# race, and skipping it would silently stop closing the previous test's pool.
# meiva:link tests/fixtures/docs/native-provisioning.meiva.okf.md
from __future__ import annotations

import importlib.machinery
import sys
import types
from collections.abc import Iterator
from typing import Any

import pytest

from tests.fixtures.loaded_modules import loaded_attribute
from tests.fixtures.provisioning import disable_loaded_event_store, quiesce_loaded_telemetry

_PROJECTION = "base.telemetry.metrics.observed_metrics"
_EMITTER = "base.telemetry.emitter"
_STORE = "base.telemetry.event_store"


def _still_importing(name: str) -> Any:
    module: Any = types.ModuleType(name)
    spec: Any = importlib.machinery.ModuleSpec(name, loader=None)
    spec._initializing = True
    module.__spec__ = spec
    return module


@pytest.fixture
def importing_telemetry_modules(
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[dict[str, types.ModuleType]]:
    modules = {name: _still_importing(name) for name in (_EMITTER, _PROJECTION, _STORE)}
    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    yield modules


def test_quiescing_skips_a_module_that_is_still_importing(
    importing_telemetry_modules: dict[str, types.ModuleType],
) -> None:
    quiesce_loaded_telemetry()
    disable_loaded_event_store()


def test_quiescing_still_closes_a_loaded_sink(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    module: Any = types.ModuleType(_PROJECTION)
    module.close_projection = lambda: calls.append("closed")
    monkeypatch.setitem(sys.modules, _PROJECTION, module)

    quiesce_loaded_telemetry()

    assert calls == ["closed"]


def test_an_attribute_lost_by_a_finished_module_stays_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    finished = types.ModuleType(_PROJECTION)
    monkeypatch.setitem(sys.modules, _PROJECTION, finished)

    with pytest.raises(AttributeError):
        loaded_attribute(_PROJECTION, "close_projection")


def test_loaded_attribute_answers_none_for_an_absent_module() -> None:
    assert loaded_attribute("no.such.module", "anything") is None
