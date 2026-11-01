# meiva:note
# UI stats face for the meiva_usage plugin.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

from base.packages.plugins.surface.schedules import StatCardSpec
from meiva_builtins.plugins.meiva_usage.default_config import UsageConfig


def contribute(config: UsageConfig) -> tuple[StatCardSpec, ...]:
    return tuple(card for c in config.collectors if c.enabled for card in c.stat_cards())
