# meiva:note
# Each history request builds its read inputs from its gateway's own configuration owner, so later
# reads observe edits to that owner and two gateways never share values.
# meiva:link gateway/agents/history/docs/configuration.meiva.okf.md

from fastapi import Request

from base.agents.history.timeline_inputs import TimelineReadInputs
from base.clock import SYSTEM_TIMERS, Clock
from base.clock.configuration import clock_config_from_boot
from base.config import ConfigBoot
from base.config.domains.agent.runtime import AgentRuntimeSettings
from base.config.service_read import ConfigAuthority
from gateway.agents.history.read_inputs import (
    TimelineRead,
    UnderstandingRead,
    understanding_chunk_ratio,
    understanding_enabled,
)

__all__ = [
    "request_timeline_read",
    "request_understanding_read",
    "timeline_read",
    "timeline_rendering",
    "understanding_read",
]


def timeline_rendering(config: ConfigBoot) -> TimelineReadInputs:
    clock = Clock(clock_config_from_boot(config), timers=SYSTEM_TIMERS)
    timestamps = config.view.general.message_timestamps
    return TimelineReadInputs(clock_factory=lambda: clock, timestamps_enabled=lambda: timestamps)


def timeline_read(config: ConfigBoot) -> TimelineRead:
    return TimelineRead(
        rendering=timeline_rendering(config),
        default_limit=config.view.display.timeline_default_limit,
        compact_history=config.view.gateway.timeline_compact_history,
    )


# meiva:note
# The understanding settings of the agent domain, or (None, None) in a profile without it.
def _agent_domain_values(config: ConfigBoot) -> tuple[float | None, bool | None]:
    if config.view.has_domain("agent"):
        return (
            config.view.agent.understanding_chunk_ratio,
            config.view.agent.understanding_enabled,
        )
    return None, None


def understanding_read(config: ConfigBoot, authority: ConfigAuthority) -> UnderstandingRead:
    chunk_ratio, enabled = _agent_domain_values(config)
    aliases = authority.read_env_aliases() if chunk_ratio is None else {}
    fields = AgentRuntimeSettings.model_fields
    return UnderstandingRead(
        rendering=timeline_rendering(config),
        hierarchy_model=config.view.lm.hierarchy_model,
        default_model=config.view.lm.llm_model,
        chunk_ratio=understanding_chunk_ratio(
            domain_value=chunk_ratio,
            env_aliases=aliases,
            field_default=fields["understanding_chunk_ratio"].default,
        ),
        enabled=understanding_enabled(
            domain_value=enabled,
            env_aliases=aliases,
            field_default=fields["understanding_enabled"].default,
        ),
    )


def request_timeline_read(request: Request) -> TimelineRead:
    return timeline_read(request.app.state.gateway_process.config)


def request_understanding_read(request: Request) -> UnderstandingRead:
    return understanding_read(
        request.app.state.gateway_process.config, request.app.state.config_authority
    )
