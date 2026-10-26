# meiva:note
# A boot ClockConfig captures only its supplied owner's timezone authority and timestamp policy, independently of older Clock handles and other owners.
# meiva:link base/clock/docs/clock.meiva.okf.md
from __future__ import annotations

from typing import Protocol

from base.clock import ClockConfig


class _GeneralDomain(Protocol):
    @property
    def timezone(self) -> str: ...

    @property
    def message_timestamp_weekday(self) -> bool: ...


class _DirectView(Protocol):
    @property
    def general(self) -> _GeneralDomain: ...


class _DynamicView(Protocol):
    def __getattr__(self, name: str) -> _GeneralDomain: ...


class _ConfigBoot(Protocol):
    @property
    def view(self) -> _DirectView | _DynamicView: ...

    def field_explicitly_set(self, name: str) -> bool: ...


def clock_config_from_boot(boot: _ConfigBoot) -> ClockConfig:
    general = boot.view.general
    return ClockConfig(
        timezone=general.timezone,
        authoritative_timezone=(
            general.timezone if boot.field_explicitly_set("timezone") else None
        ),
        message_timestamp_weekday=general.message_timestamp_weekday,
    )
