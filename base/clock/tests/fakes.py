from __future__ import annotations

from zoneinfo import ZoneInfo

import pytest

from base.clock import Clock


def fix_zone(monkeypatch: pytest.MonkeyPatch, zone: ZoneInfo | None) -> None:

    def fixed(_clock: Clock) -> ZoneInfo | None:
        return zone

    monkeypatch.setattr(Clock, "zone", fixed)
