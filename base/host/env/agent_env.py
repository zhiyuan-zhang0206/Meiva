# meiva:note
# The environment variables that carry an agent's config overlay and birth stamp to a child.
#
# They travel as JSON in env vars, never argv (issue #974: argv is world-readable via `ps`). They
# are not Settings fields: they are process-bound carriers the exec launcher writes and the child
# boot consumes.

from __future__ import annotations

__all__ = ["AGENT_BIRTH_CONFIG_ENV", "AGENT_CONFIG_OVERLAY_ENV"]

AGENT_CONFIG_OVERLAY_ENV = "MEIVA_AGENT_CONFIG_OVERLAY"
AGENT_BIRTH_CONFIG_ENV = "MEIVA_AGENT_BIRTH_CONFIG"
