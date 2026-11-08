# meiva:note
# Shared, fixture-free helpers for the admission gate's tests: Recorder stands in for the executor.
from __future__ import annotations

from collections.abc import Sequence


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str], context: dict[str, object] | None = None) -> int:
        self.calls.append(list(command))
        return 0
