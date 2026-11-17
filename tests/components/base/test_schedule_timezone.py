from __future__ import annotations

import ast
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo, available_timezones

import pytest
from croniter import croniter

from base.clock.recurring import next_fire
from base.daemon.schedules.builtin_schedules import BuiltinSchedule, load_manifest

_SCHEDULES = Path(__file__).resolve().parents[3] / "schedules"
_BUILTINS = load_manifest(_SCHEDULES / "manifest.json")
_CRON_BUILTINS = [entry for entry in _BUILTINS if entry.trigger_kind == "cron"]
_IANA = available_timezones()
assert "Asia/Shanghai" in _IANA, "no tz database available; the zone-name check cannot run"


def _tree(entry: BuiltinSchedule) -> ast.Module:
    return ast.parse((_SCHEDULES / entry.script).read_text(encoding="utf-8"))


@pytest.mark.parametrize(
    "entry",
    _BUILTINS,
    ids=lambda entry: entry.name,
)
def test_builtin_manifest_declares_cluster_clock_trigger(entry: BuiltinSchedule) -> None:
    assert entry.timezone is None, f"{entry.name} overrides the cluster timezone"
    if entry.trigger_kind == "cron":
        assert entry.cron_expr is not None
        assert entry.interval_seconds is None
    else:
        assert entry.trigger_kind == "every"
        assert entry.cron_expr is None
        assert entry.interval_seconds is not None


@pytest.mark.parametrize("entry", _BUILTINS, ids=lambda entry: entry.name)
def test_no_template_hardcodes_an_iana_timezone(entry: BuiltinSchedule) -> None:
    literals = [
        node.value
        for node in ast.walk(_tree(entry))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    named = [v for v in literals if "/" in v and v in _IANA]
    assert named == [], f"{entry.script} hard-codes {named}"


@pytest.mark.parametrize("tz", ["Asia/Shanghai", "America/Los_Angeles", "Asia/Kathmandu"])
@pytest.mark.parametrize("entry", _CRON_BUILTINS, ids=lambda entry: entry.name)
def test_manifest_crons_fire_on_cluster_wall_clock(entry: BuiltinSchedule, tz: str) -> None:
    assert entry.cron_expr is not None
    base = datetime(2026, 6, 9, tzinfo=UTC)
    fire = next_fire(entry.cron_expr, after=base, timezone=tz)
    local = fire.astimezone(ZoneInfo(tz))
    assert croniter.match(entry.cron_expr, local), f"{entry.name}: {entry.cron_expr}"


def test_same_cron_yields_different_instants_per_cluster_timezone() -> None:
    base = datetime(2026, 6, 9, tzinfo=UTC)
    shanghai = next_fire("0 4 * * *", after=base, timezone="Asia/Shanghai")
    pacific = next_fire("0 4 * * *", after=base, timezone="America/Los_Angeles")
    assert shanghai == datetime(2026, 6, 9, 20, 0, tzinfo=UTC)
    assert pacific == datetime(2026, 6, 9, 11, 0, tzinfo=UTC)


def test_weekly_cron_lands_on_the_cluster_weekday() -> None:
    base = datetime(2026, 6, 9, tzinfo=UTC)
    fire = next_fire("0 0 * * 2", after=base, timezone="Asia/Shanghai")
    assert fire.astimezone(ZoneInfo("Asia/Shanghai")).weekday() == 1
    assert fire.astimezone(ZoneInfo("America/Los_Angeles")).weekday() == 0
