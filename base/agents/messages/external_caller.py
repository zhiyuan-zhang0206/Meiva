"""Opt-in external tool identity inherited once by its subprocess tree.

MEIVA_CALLER_IDENTITY is asserted provenance, not credentials. It declares an
external agent and never stands for the human, system, or an Meiva agent whose
environment was inherited.
"""

from __future__ import annotations

import os
import shlex
from collections.abc import Mapping

from base.api_contracts.caller_identity import CallerIdentity


def external_caller(*, env: Mapping[str, str] | None = None) -> CallerIdentity | None:
    """Read a strictly validated explicit external profile, or no opt-in."""
    # env-ok: external subprocess provenance handoff, not cluster configuration
    source_env = os.environ if env is None else env
    raw = source_env.get("MEIVA_CALLER_IDENTITY")
    if raw is None:
        return None
    return CallerIdentity.model_validate_json(raw)


def launch_caller_assignment(tool: str, instance: str | None) -> str:
    """Optional shell-safe launch assignment; no opt-in means no activation."""
    if instance is None:
        return ""
    caller = CallerIdentity(kind="external_agent", subject=tool, instance=instance)
    return f"MEIVA_CALLER_IDENTITY={shlex.quote(caller.model_dump_json(exclude_none=True))} "
