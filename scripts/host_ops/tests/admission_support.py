# meiva:note
# Shared, fixture-free helpers for the admission gate's tests: `run_admission` drives
# `admission.main` with injected readers and a recording executor, `collect_reader` and
# `BudgetSequence` are the two budget readers, and `Recorder` stands in for the executor.
# They live here because both test_admission_run.py and test_admission_floor.py drive the
# same entry point, and the 800-line file budget keeps either test module from owning them.

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from scripts.host_ops import admission_run as admission


def collect_reader(
    free_gib: float = 26.1,
    pressure: int = 1,
    unstable: bool = False,
    source: str = "disk",
):
    def reader() -> admission.Reading:
        return admission.Reading(
            free_gib=free_gib, source=source, unstable=unstable, detail="df 84.0%"
        )

    return reader, lambda: pressure


class Recorder:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, command: Sequence[str], context: dict[str, object]) -> int:
        self.calls.append(list(command))
        return 0


class BudgetSequence:
    # meiva:note
    # A budget reader that answers with a chosen sequence: the last value repeats, so a test
    # states only the readings that matter and `calls` proves how many the gate took.
    def __init__(self, values: Sequence[float], source: str = "disk") -> None:
        self.remaining = list(values)
        self.source = source
        self.calls: list[float] = []

    def __call__(self) -> admission.Reading:
        chosen = self.remaining.pop(0) if len(self.remaining) > 1 else self.remaining[0]
        self.calls.append(chosen)
        return admission.Reading(
            free_gib=chosen, source=self.source, unstable=False, detail="df 84.0%"
        )


def run_admission(
    argv: list[str],
    *,
    free_gib: float = 26.1,
    pressure: int = 1,
    unstable: bool = False,
    source: str = "disk",
    tmp_path: Path,
    readings: Sequence[float] | None = None,
) -> tuple[int, Recorder]:
    recorder = Recorder()
    if readings is None:
        disk, pressure_reader = collect_reader(free_gib, pressure, unstable, source)
    else:
        disk, pressure_reader = BudgetSequence(readings, source), lambda: pressure
    code = admission.main(
        argv,
        budget_reader=disk,
        pressure_reader=pressure_reader,
        psi_reader=lambda: None,
        gpu_reader=lambda: None,
        executor=recorder,
        home=tmp_path,
        sleeper=lambda _seconds: None,
    )
    return code, recorder
