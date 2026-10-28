# meiva:note
# The zone a schedule-verification report is written in. The instant is a fact (UTC); the zone is a
# configuration choice, and the two only meet when a person reads the stamp. These tests pin that
# the choice comes from the configuration the invocation carries — not from the machine the process
# happens to run on, which is what `astimezone(None)` would silently fall back to.
from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast
from zoneinfo import ZoneInfo

from base.clock.tests.manual_timers import ManualTimers
from cli.commands.management.schedules_verify import stamped_instant
from cli.database.runtime import CommandRuntime

_INSTANT = datetime(2026, 10, 29, 1, 0, tzinfo=UTC)


# meiva:note
# A configuration boot that answers only what the runtime's zone asks of it: the timezone it names
# and whether the invocation named it. `explicit` is `field_explicitly_set("timezone")` — a
# defaulted timezone is not an authority, so the runtime answers None for it.
def _boot(timezone_name: str, *, explicit: bool = True) -> Any:
    general = SimpleNamespace(timezone=timezone_name, message_timestamp_weekday=False)

    def field_explicitly_set(name: str) -> bool:
        return explicit and name == "timezone"

    return SimpleNamespace(
        view=SimpleNamespace(general=general),
        field_explicitly_set=field_explicitly_set,
    )


def _instant() -> datetime:
    return _INSTANT


def _runtime(timezone_name: str, *, explicit: bool = True) -> CommandRuntime:
    # meiva:note
    # The zone is the runtime's own answer, taken from the property rather than written here: a
    # change that made it answer None would move the stamp to the machine's zone and fail the test
    # below, which is the regression this file exists to catch.
    stub = SimpleNamespace(
        config=_boot(timezone_name, explicit=explicit),
        timers=ManualTimers(),
        clock=SimpleNamespace(now=_instant),
    )
    getter = CommandRuntime.zone.fget
    assert getter is not None, "the runtime's zone must stay a readable property"
    stub.zone = getter(stub)
    return cast(CommandRuntime, stub)


def test_the_report_is_stamped_in_the_zone_the_configuration_names() -> None:
    # meiva:note
    # The named zone itself is what is asserted, not its offset: `astimezone(None)` — the shape
    # where the runtime answers None — yields a fixed-offset timezone from the machine's clock, so
    # it fails here even on a machine that happens to sit at the same offset as the configuration.
    # An offset comparison alone would pass on such a machine, which is how the regression this
    # file guards against slipped through review the first time.
    stamped = stamped_instant(_runtime("Asia/Shanghai"))

    assert stamped.tzinfo == ZoneInfo("Asia/Shanghai")
    assert stamped.utcoffset() == timedelta(hours=8)
    assert stamped.astimezone(UTC) == _INSTANT


def test_an_invocation_that_names_no_zone_leaves_the_choice_to_the_machine() -> None:
    # meiva:note
    # A defaulted timezone is not an authority: the runtime answers None, and the stamp falls to the
    # machine's zone exactly as it did before this change.
    runtime = _runtime("Asia/Shanghai", explicit=False)

    assert runtime.zone is None
    assert stamped_instant(runtime).tzinfo != ZoneInfo("Asia/Shanghai")
    assert stamped_instant(runtime).astimezone(UTC) == _INSTANT


def test_the_runtime_zone_is_a_zone_the_configuration_names() -> None:
    assert _runtime("Asia/Shanghai").zone == ZoneInfo("Asia/Shanghai")
    assert _runtime("Not/AZone").zone is None
