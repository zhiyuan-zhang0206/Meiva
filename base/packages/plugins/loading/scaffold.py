# meiva:note
# What the explicit `meiva memory init` command hands every enabled plugin's `scaffold(*, machine,
# facts)`: the values a scaffold used to read from the process settings itself, read once by the
# command that runs it. A scaffold is host-side provisioning that depends on `base` only, so the
# type lives here; it holds the Meiva home the plugin's checkouts sit under, the two memory
# provisioning settings, and how to reach the gateway when a runner bootstraps its pool from it.
# meiva:link docs/conventions/extensions/plugin-spec-v2.md

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from base.cluster.machine import GatewayAccess

__all__ = ["ScaffoldFacts"]


@dataclass(frozen=True)
class ScaffoldFacts:
    home: Path
    memory_keep_local: bool
    memory_remote: str
    gateway: GatewayAccess
