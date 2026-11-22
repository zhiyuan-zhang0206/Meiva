"""Fleet policy configuration."""

from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.extensions import PluginContributions


class FleetConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    reduce_context_switch: bool = Field(
        title="Reduce interruptions to the user",
        default=True,
        description=(
            "Platform-wide reduce-context-switch default (user ruling 2026-09-20): "
            "inject the 'Reduce context switch for the human' system-prompt section — "
            "queue-never-push, one update per manager, milestone "
            "cadence, authorization/decision direct, out-of-band push only for a true "
            "emergency. False is the escape hatch back to the pre-platform behavior: "
            "the section is not injected and the reduce-context-switch policy keys "
            "do not apply."
        ),
        json_schema_extra={
            "env_var": "MEIVA_REDUCE_CONTEXT_SWITCH",
            "restart_required": "agent",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "capability": "agent-runner",
        },
    )


# meiva:note
# Declare Fleet's configuration schema; Fleet reads no Core flag.
def contribute() -> PluginContributions:
    return PluginContributions(config=FleetConfig)
