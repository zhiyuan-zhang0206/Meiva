from __future__ import annotations

from base.lm.harness.cancel import CancelReport
from base.lm.harness.claude.auth import (
    SECRET_ENV_NAMES,
    AuthConfig,
    AuthMode,
    MissingApiKeyError,
    build_env,
    describe_auth,
)
from base.lm.harness.claude.config import DEFAULT_MODEL, BridgeConfig
from base.lm.harness.session import SessionTotals, TurnOutcome

# meiva:note
# The package door re-exports the pure half only. `ClaudeSession` and the errors it exports are
# the adapter's effect shell, so a caller imports them from `base.lm.harness.claude.session`.
# A door that is `strict` may re-export only `strict` modules.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
__all__ = [
    "DEFAULT_MODEL",
    "SECRET_ENV_NAMES",
    "AuthConfig",
    "AuthMode",
    "BridgeConfig",
    "CancelReport",
    "MissingApiKeyError",
    "SessionTotals",
    "TurnOutcome",
    "build_env",
    "describe_auth",
]
