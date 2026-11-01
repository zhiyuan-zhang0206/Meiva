from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass

__all__ = ["StoredConfig", "config_fingerprint"]


# meiva:note
# JSONB key order must not invalidate a cached runtime. Keep the two columns
# separate and preserve the string coercion of non-JSON decoded scalars.
# meiva:link services/agent_runner/agent_host/lifecycle/docs/configuration.meiva.okf.md
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
# meiva:link services/agent_runner/agent_host/lifecycle/docs/configuration.meiva.okf.md
@dataclass
class StoredConfig:
    machine: str
    status: str
    harness: str
    config_overlay: dict[str, object] | None
    birth_config: dict[str, object] | None

    @property
    def fingerprint(self) -> str:
        return config_fingerprint(self.config_overlay, self.birth_config, harness=self.harness)
