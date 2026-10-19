from __future__ import annotations

from pathlib import Path

from meiva_builtins.plugins.meiva_fleet.task_maintenance import daemon


def test_daemon_guard_checks_only_current_pidfile() -> None:
    calls: list[tuple[Path, str]] = []

    def holds_daemon(path: Path, module: str) -> bool:
        calls.append((path, module))
        return False

    assert daemon._is_running(holds=holds_daemon) is False
    assert calls == [(daemon._pidfile(), daemon.__name__)]
