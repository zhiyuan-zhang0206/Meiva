# meiva:note
# Schedules face for the meiva_usage plugin.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import json

from base.packages.plugins.surface.schedules import ScheduleSpec
from meiva_builtins.plugins.meiva_usage.default_config import UsageConfig


def contribute(config: UsageConfig) -> tuple[ScheduleSpec, ...]:
    specs: list[ScheduleSpec] = []
    for c in config.collectors:
        if not c.enabled:
            continue
        spec_json = json.dumps(c.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
        script = (
            f"# collector_spec: {spec_json}\n"
            "from meiva_builtins.plugins.meiva_usage.runner import run_from_schedule\n\n"
            f"run_from_schedule(globals().get('MEIVA_SCHEDULE_INPUTS'), collector_id={c.id!r})\n"
        )
        specs.append(
            ScheduleSpec(
                name=f"meiva_usage:{c.id}",
                machine=c.machine,
                description=f"{c.label} ({c.provider} usage collector)",
                script=script,
                command="python schedule.py",
                secret_env_vars=c.secret_env_vars(),
                default_enabled=c.enabled,
            )
        )
    return tuple(specs)
