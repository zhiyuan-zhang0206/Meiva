from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from loguru import logger

from base.host.memory_pressure import PressureLevel
from services.agent_runner.exec_host.memory_guard import ExecDomain, ExecMemoryGuard

_GIB = 1024**3
_DEAD_PIDS = iter(range(900_001, 900_100))


def _notice(result_path: Path) -> Path:
    return result_path.with_name(result_path.name + ".host-kill")


class _Source:
    def __init__(self, *levels: PressureLevel) -> None:
        self.levels = list(levels)

    def pressure(self) -> PressureLevel:
        return self.levels.pop(0) if len(self.levels) > 1 else self.levels[0]

    def footprint(self, pid: int) -> int:
        return 0


def _domain(tmp_path: Path, gib: float, pid: int | None = None) -> ExecDomain:
    return ExecDomain(
        pid=next(_DEAD_PIDS) if pid is None else pid,
        create_time=1.0,
        agent_id=7,
        result_path=tmp_path / f"res-{pid}.json",
        footprint=int(gib * _GIB),
    )


def _guard(source: _Source, domains: list[ExecDomain], *, floor: int = _GIB) -> ExecMemoryGuard:
    return ExecMemoryGuard(source, domains=lambda: list(domains), floor_bytes=floor)


@pytest.mark.parametrize("level", ["normal", "warning"])
def test_below_critical_never_looks_at_the_execs(tmp_path: Path, level: PressureLevel) -> None:
    called: list[str] = []
    guard = ExecMemoryGuard(
        _Source(level),
        domains=lambda: called.append("listed") or [_domain(tmp_path, 9.0)],
        floor_bytes=_GIB,
    )
    assert guard.check_once() is None
    assert called == []


def test_a_domain_at_or_below_the_floor_is_reported_but_not_killed(tmp_path: Path) -> None:
    records: list[Any] = []
    sink = logger.add(lambda message: records.append(message.record))
    at_floor, below = _domain(tmp_path, 1.0), _domain(tmp_path, 0.1)
    try:
        assert _guard(_Source("critical"), [at_floor]).check_once() is None
        assert _guard(_Source("critical"), [below]).check_once() is None
    finally:
        logger.remove(sink)
    skips = [row for row in records if row["extra"].get("event") == "exec_memory_guard_skipped"]
    assert [row["extra"]["footprint_bytes"] for row in skips] == [int(1.0 * _GIB), int(0.1 * _GIB)]
    assert [row["level"].name for row in skips] == ["INFO", "INFO"]
    assert {row["extra"]["floor_bytes"] for row in skips} == {_GIB}
    assert all(isinstance(row["extra"]["free_swap_bytes"], int) for row in skips)
    assert not _notice(at_floor.result_path).exists()
    assert not _notice(below.result_path).exists()


def test_a_domain_above_the_floor_is_killed_with_its_notice(tmp_path: Path) -> None:
    victim = _domain(tmp_path, 2.0)
    assert _guard(_Source("critical"), [victim]).check_once() == victim
    assert _notice(victim.result_path).exists()


def test_many_small_execs_under_critical_pressure_do_nothing(tmp_path: Path) -> None:
    domains = [_domain(tmp_path, 0.3) for _ in range(3)]
    guard = _guard(_Source("critical"), domains)
    assert guard.check_once() is None
    assert all(not _notice(domain.result_path).exists() for domain in domains)


def test_the_same_skip_is_reported_once_per_episode(tmp_path: Path) -> None:
    records: list[Any] = []
    sink = logger.add(lambda message: records.append(message.record))
    guard = _guard(_Source("critical", "critical", "normal", "critical"), [_domain(tmp_path, 0.1)])
    try:
        for _ in range(4):
            guard.check_once()
    finally:
        logger.remove(sink)
    skips = [row for row in records if row["extra"].get("event") == "exec_memory_guard_skipped"]
    assert len(skips) == 2


def test_a_new_largest_exec_is_reported_again(tmp_path: Path) -> None:
    records: list[Any] = []
    sink = logger.add(lambda message: records.append(message.record))
    domains = [_domain(tmp_path, 0.1)]
    guard = _guard(_Source("critical"), domains)
    try:
        guard.check_once()
        domains.clear()
        domains.append(_domain(tmp_path, 0.2))
        guard.check_once()
    finally:
        logger.remove(sink)
    skips = [row for row in records if row["extra"].get("event") == "exec_memory_guard_skipped"]
    assert [row["extra"]["footprint_bytes"] for row in skips] == [int(0.1 * _GIB), int(0.2 * _GIB)]
