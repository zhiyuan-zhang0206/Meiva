from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path

from base.deploy.lifecycle import start_serving

_ORIG_IS_SERVING = start_serving.is_serving
_ORIG_RECOVERY = start_serving.recovery_permitted
_gate: list[Path] = []


def clear_fixture_gate() -> None:
    _gate.clear()


def inject_fixture_gate(gate: Path) -> None:
    _gate[:] = [gate]

    def fixture_is_serving(home: Path) -> bool:
        if not _gate:
            return _ORIG_IS_SERVING(home)
        return _gate[0].is_file()

    @contextmanager
    def fixture_recovery(home: Path) -> Generator[bool]:
        if not _gate:
            with _ORIG_RECOVERY(home) as permitted:
                yield permitted
            return
        yield fixture_is_serving(home)

    start_serving.is_serving = fixture_is_serving
    start_serving.recovery_permitted = fixture_recovery
