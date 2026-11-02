# meiva:note
# The to-user read caps, as one frozen value a composition root hands down. A panel document is
# read under four bounds: its own bytes, the bytes of one referenced file, how many references it
# may name, and how deep a reference chain may go. Every read entry point takes this value, so
# the reading side never reaches for the process settings and a test can drive a cap the same way
# it drives the file.

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from base.config.service_read import ConfigAuthority
from base.host.env.agent_slices import AgentSlices


# meiva:note
# The four `settings.to_user` fields this value carries, named as the domain model states them.
class ToUserLimitFields(Protocol):
    max_document_bytes: int
    max_references: int
    max_chain_depth: int
    max_resource_bytes: int


# meiva:note
# One configuration's four read-side caps.
@dataclass(frozen=True, slots=True)
class ToUserLimits:
    document_bytes: int
    references: int
    chain_depth: int
    resource_bytes: int


# meiva:note
# Take the caps from a configuration read model's `to_user` domain.
def to_user_limits(fields: ToUserLimitFields) -> ToUserLimits:
    return ToUserLimits(
        document_bytes=fields.max_document_bytes,
        references=fields.max_references,
        chain_depth=fields.max_chain_depth,
        resource_bytes=fields.max_resource_bytes,
    )


# meiva:note
# The caps an ops root derives from its read model: the complete model, not this process's
# profile, so the listener applies the same bounds the agent's own reader would.
def to_user_limits_from_read_model(model: ConfigAuthority) -> ToUserLimits:
    return ToUserLimits(
        document_bytes=model.service_field_value("max_document_bytes"),
        references=model.service_field_value("max_references"),
        chain_depth=model.service_field_value("max_chain_depth"),
        resource_bytes=model.service_field_value("max_resource_bytes"),
    )


# meiva:note
# The caps an agent's turn reads from its resolved slices: the same per-turn configuration that
# answers `workspace_in_system_prompt` and the panel's own word limit, so a panel read applies the
# bounds the process that renders it booted with, with a per-agent pin winning if one exists.
def to_user_limits_from_agent(agent: AgentSlices) -> ToUserLimits:
    return ToUserLimits(
        document_bytes=agent.read("to_user", "max_document_bytes"),
        references=agent.read("to_user", "max_references"),
        chain_depth=agent.read("to_user", "max_chain_depth"),
        resource_bytes=agent.read("to_user", "max_resource_bytes"),
    )
