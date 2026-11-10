from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

__all__ = ["StoredConfig", "config_fingerprint"]


# meiva:note
# JSONB key order must not invalidate a cached runtime. Keep the two columns
# separate and preserve the string coercion of non-JSON decoded scalars.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/configuration.md
def config_fingerprint(
    config_overlay: dict[str, object] | None,
    birth_config: dict[str, object] | None,
    harness: str | None = None,
) -> str:
    blob = json.dumps(
        {"birth": birth_config, "harness": harness, "overlay": config_overlay},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


# meiva:note
# The stored row stays mutable and retains the supplied maps, including nested
# aliases. Its fingerprint is evaluated from those maps when requested.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/configuration.md
@dataclass
class StoredConfig:
    machine: str
    status: str
    harness: str
    config_overlay: dict[str, object] | None
    birth_config: dict[str, object] | None
    # meiva:note
    # The fork source, only while it runs on this agent's current harness: a child switched to another
    # harness has no session of the source's to clone, so the id reads None there.
    # meiva:link base/lm/harness/docs/fork.md
    fork_source_agent_id: int | None = None

    @property
    def fingerprint(self) -> str:
        return config_fingerprint(self.config_overlay, self.birth_config, harness=self.harness)
