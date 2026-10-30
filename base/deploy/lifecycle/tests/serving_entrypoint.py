# meiva:note
# The fixture replaces the serving gate for the whole process, so it has to answer the same call
# the production readers make -- including the `observe` keyword a caller names to read the root
# birth its own way. A replacement with the older one-argument shape does not fail here; it fails
# later, in whichever test runs next in the same process, as
# `fixture_is_serving() got an unexpected keyword argument 'observe'` (2026-10-30: the merge
# queue caught it when a lifecycle test and a gateway roster test landed in one shard).
from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from base.deploy.lifecycle import start_serving
from base.deploy.lifecycle.start_serving import RootBirth

_ORIG_IS_SERVING = start_serving.is_serving
_ORIG_RECOVERY = start_serving.recovery_permitted
_gate: list[Path] = []


def clear_fixture_gate() -> None:
    _gate.clear()


def inject_fixture_gate(gate: Path) -> None:
    _gate[:] = [gate]

    def fixture_is_serving(
        home: Path, *, observe: Callable[[Path], RootBirth] = start_serving.observe_root
    ) -> bool:
        if not _gate:
            return _ORIG_IS_SERVING(home, observe=observe)
        return _gate[0].is_file()

    @contextmanager
    def fixture_recovery(
        home: Path, *, observe: Callable[[Path], RootBirth] = start_serving.observe_root
    ) -> Generator[bool]:
        if not _gate:
            with _ORIG_RECOVERY(home, observe=observe) as permitted:
                yield permitted
            return
        yield fixture_is_serving(home, observe=observe)

    start_serving.is_serving = fixture_is_serving
    start_serving.recovery_permitted = fixture_recovery
