"""Cluster-timezone resolution + process-wide application tests.

Covers the one-cluster-clock boot hook (Task #1758, user ruling 2026-08-27):
a process holding an authoritative MEIVA_TIMEZONE applies it as its own TZ on
POSIX; a process without one (settings-lite / bare checkout) is untouched.
"""

from __future__ import annotations

import os
import time
from collections.abc import Generator
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.config import (
    ConfigBoot,
    apply_cluster_timezone,
    cluster_tz_name,
    host_tz_name,
)


@pytest.fixture(autouse=True)
def _restore_process_tz() -> Generator[None, None, None]:
    """Restore the process's original TZ env + tzset state after each test.

    apply_cluster_timezone mutates the process wall clock (os.environ["TZ"]
    assignment + time.tzset()). A plain ``os.environ["TZ"] = ...`` assignment
    is NOT tracked by pytest's monkeypatch (it only restores what setenv /
    delenv registered — a delenv on an absent key registers nothing), so the
    assignment must be undone here: restore the pre-test TZ value, then
    re-tzset() so the C library agrees with the env again. Without this, a
    leaked TZ is applied to the process wall clock on a UTC host (gmtoff
    0 -> 28800); a CST host masked it (QA #737).
    """
    original_tz = os.environ.get("TZ")
    yield
    if original_tz is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = original_tz
    time.tzset()


def _clock_for(owner: ConfigBoot) -> Clock:
    return Clock(
        ClockConfig(
            timezone=owner.view.general.timezone,
            authoritative_timezone=cluster_tz_name(owner),
            message_timestamp_weekday=owner.view.general.message_timestamp_weekday,
        ),
        timers=SYSTEM_TIMERS,
    )


def test_cluster_tz_returns_zoneinfo_when_authoritative() -> None:
    owner = ConfigBoot(
        environment={"MEIVA_CONFIG_FETCH": "skip", "MEIVA_TIMEZONE": "Asia/Shanghai"}
    )
    assert _clock_for(owner).zone() == ZoneInfo("Asia/Shanghai")


def test_cluster_tz_none_without_authoritative_value() -> None:
    owner = ConfigBoot(environment={"MEIVA_CONFIG_FETCH": "skip"})
    assert _clock_for(owner).zone() is None


def test_cluster_tz_none_for_invalid_name() -> None:
    """Belt and braces: Settings already fails fast on a bad IANA name at
    construction, but a display path must degrade to the host zone rather
    than crash if one ever slips through."""
    owner = ConfigBoot(environment={"MEIVA_CONFIG_FETCH": "skip"})
    owner.set_field("timezone", "Not/AZone")
    assert _clock_for(owner).zone() is None


def test_apply_sets_tz_and_rezones_posix() -> None:
    """With an authoritative MEIVA_TIMEZONE, the process wall clock follows the
    cluster zone: os.environ["TZ"] is set and time.localtime() re-resolves to
    it (tm_gmtoff is the observable)."""
    owner = ConfigBoot(
        environment={"MEIVA_CONFIG_FETCH": "skip", "MEIVA_TIMEZONE": "Asia/Shanghai"}
    )
    apply_cluster_timezone(owner)
    assert os.environ["TZ"] == "Asia/Shanghai"
    assert time.localtime().tm_gmtoff == 8 * 3600


def test_apply_noop_without_authoritative_value() -> None:
    """A process without an authoritative value is left untouched — forcing
    the field default (America/Los_Angeles) onto a settings-lite maintenance
    verb or a bare checkout would be wrong."""
    owner = ConfigBoot(environment={"MEIVA_CONFIG_FETCH": "skip"})
    before = os.environ.get("TZ")
    apply_cluster_timezone(owner)
    assert os.environ.get("TZ") == before


def test_host_tz_name_resolves_zoneinfo_symlink(tmp_path: Path) -> None:
    """/etc/localtime -> /usr/share/zoneinfo/Asia/Shanghai resolves to the
    IANA name."""
    zone_file = tmp_path / "usr" / "share" / "zoneinfo" / "Asia" / "Shanghai"
    zone_file.parent.mkdir(parents=True)
    zone_file.write_bytes(b"")
    link = tmp_path / "localtime"
    link.symlink_to(zone_file)
    assert host_tz_name(str(link)) == "Asia/Shanghai"


def test_host_tz_name_falls_back_to_utc(tmp_path: Path) -> None:
    """No zoneinfo symlink or an unreadable link yields UTC."""
    broken_loop = tmp_path / "loop"
    broken_loop.symlink_to(broken_loop)
    assert host_tz_name(str(broken_loop)) == "UTC"

    var_zone = tmp_path / "var" / "db" / "timezone" / "zoneinfo" / "Asia" / "Shanghai"
    var_zone.parent.mkdir(parents=True)
    var_zone.write_bytes(b"")
    var_link = tmp_path / "var-localtime"
    var_link.symlink_to(var_zone)
    assert host_tz_name(str(var_link)) == "Asia/Shanghai"

    plain_file = tmp_path / "plain-localtime"
    plain_file.write_bytes(b"")
    assert host_tz_name(str(plain_file)) == "UTC"
