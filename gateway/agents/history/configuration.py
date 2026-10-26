# meiva:note
# What one history request reads from the gateway's configuration, as values: the config the
# gateway app was built with, plus the cluster's `.env` for the understanding options a
# gateway-profile process does not hold.
# meiva:link gateway/agents/history/docs/configuration.meiva.okf.md

from fastapi import Request

from base.agents.messages.timestamp_policy import TimestampPolicy
from base.clock import Clock
from base.config.domains.agent.runtime import AgentRuntimeSettings
from base.config.service_read import ConfigAuthority
from gateway.agents.history.read_inputs import (
    TimelineRead,
    UnderstandingRead,
    understanding_chunk_ratio,
    understanding_enabled,
)
from gateway.http.config import GatewayConfig, gateway_clock, gateway_config

__all__ = [
    "request_timeline_read",
    "request_understanding_read",
    "timeline_read",
    "timeline_rendering",
    "understanding_read",
]


def timeline_rendering(config: GatewayConfig, clock: Clock) -> TimestampPolicy:
    return TimestampPolicy.from_clock(clock, timestamps_enabled=config.message_timestamps)


def timeline_read(config: GatewayConfig, clock: Clock) -> TimelineRead:
    return TimelineRead(
        rendering=timeline_rendering(config, clock),
        default_limit=config.display.timeline_default_limit,
        compact_history=config.timeline_compact_history,
    )


def understanding_read(
    config: GatewayConfig, authority: ConfigAuthority, clock: Clock
) -> UnderstandingRead:
    domain = config.understanding
    aliases = authority.read_env_aliases() if domain.chunk_ratio is None else {}
    fields = AgentRuntimeSettings.model_fields
    return UnderstandingRead(
        rendering=timeline_rendering(config, clock),
        hierarchy_model=config.models.hierarchy_model,
        default_model=config.models.llm_model,
        chunk_ratio=understanding_chunk_ratio(
            domain_value=domain.chunk_ratio,
            env_aliases=aliases,
            field_default=fields["understanding_chunk_ratio"].default,
        ),
        enabled=understanding_enabled(
            domain_value=domain.enabled,
            env_aliases=aliases,
            field_default=fields["understanding_enabled"].default,
        ),
    )


def request_timeline_read(request: Request) -> TimelineRead:
    return timeline_read(gateway_config(request), gateway_clock(request))


def request_understanding_read(request: Request) -> UnderstandingRead:
    return understanding_read(
        gateway_config(request), request.app.state.config_authority, gateway_clock(request)
    )
