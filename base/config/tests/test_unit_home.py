from __future__ import annotations

from pathlib import Path

from base.config import Settings
from base.config.base import unit_home as resolve_unit_home
from base.paths.home import resolve_meiva_home


def test_unit_home_follows_meiva_home(unit_home: Path) -> None:
    assert resolve_unit_home() == unit_home


def test_unit_home_defaults_to_home_meiva() -> None:
    assert resolve_unit_home() == resolve_meiva_home()


def test_pidfile_fields_rooted_under_unit_home() -> None:
    root = resolve_unit_home()
    s = Settings()
    assert s.services.gateway_pidfile == root / "run" / "gateway.pid"
    assert s.services.memory_root == root / "memory"
    assert s.services.memory_search_data_dir == root / "memory-search"
