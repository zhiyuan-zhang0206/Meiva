# meiva:note
# What an SDK call is metered as, decided over values the recorder read: how the namespace walk
# treats a member it found, the name an MCP tool call is metered under and the usage identity a
# call is stamped with. The metering shell (`metering.py`) walks the live namespace and reads the
# bound context; these functions only choose.
# meiva:link meiva/docs/sdk-surface/sdk-surface.meiva.okf.md

from __future__ import annotations

import inspect
from enum import Enum, auto
from types import ModuleType, SimpleNamespace

from base.agents.context.identity import AgentIdentity
from base.api_contracts.caller_identity import CallerIdentity
from meiva.sdk_surface.pure.caller.identity_plan import caller_facts
from meiva.sdk_surface.pure.caller.usage_plan import usage_identity


class MemberKind(Enum):
    ROUTINE = auto()
    NAMESPACE = auto()
    OTHER = auto()


# meiva:note
# How the namespace walk treats a member it found without evaluating it: a routine is a metering
# target, a module or plain namespace is walked into, anything else (a class, a constant) is
# skipped.
def member_kind(member: object) -> MemberKind:
    if inspect.isroutine(member):
        return MemberKind.ROUTINE
    if isinstance(member, (ModuleType, SimpleNamespace)):
        return MemberKind.NAMESPACE
    return MemberKind.OTHER


# meiva:note
# The name an MCP tool call is metered under: the runtime server and tool, because the dynamic
# MCP surface cannot be walked.
def mcp_call_name(server: str, tool: str) -> str:
    return f"mcps.{server}.{tool}"


# meiva:note
# The usage identity an SDK call is stamped with, from the identity the caller's context carries
# (`own`, None for an unbound caller, which is a system caller) and the installation's external
# caller profile. A borrowed lease attributes the call to the leased agent, ahead of the owner and
# of the external profile.
def call_identity(own: AgentIdentity | None, external: CallerIdentity | None) -> dict[str, object]:
    borrowed = own.lease.agent_id if own is not None and own.lease is not None else None
    return usage_identity(
        borrowed, caller_facts(own), None if external is None else external.source()
    )
