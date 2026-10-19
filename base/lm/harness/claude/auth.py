from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

SECRET_ENV_NAMES: tuple[str, ...] = ("ANTHROPIC_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN")


class MissingApiKeyError(RuntimeError):
    pass


class AuthMode(StrEnum):
    OAUTH = "oauth"
    API_KEY = "api_key"


@dataclass(frozen=True, slots=True)
class AuthConfig:
    mode: AuthMode = AuthMode.OAUTH
    config_dir: Path | None = None
    api_key_env: str = "ANTHROPIC_API_KEY"


# meiva:note
# The child environment is built, not inherited: the SDK merges `env` over the
# parent process environment, so "unset" has to be an explicit empty value.
# Blanking both credential variables is what keeps an exported API key from
# silently moving a subscription session onto metered billing. API-key mode
# restores exactly the one variable the caller named; nothing here reads, stores
# or forwards a secret, which is also why `describe_auth` reports presence only.
#
# What the caller hands in rides along, because the merge makes this a patch rather
# than a whole environment: `wiring.bridge_for` adds the session's registration
# handle (`MEIVA_HARNESS_HANDLE`) and the runner's home allowlist on top of it, and
# dropping them left every shell the CLI spawned without an identity — a live turn
# read `meiva.self.AGENT_ID` as `None` inside its own Bash tool, so the agent could
# not call the SDK as itself from a tool.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
def build_env(environ: Mapping[str, str], auth: AuthConfig) -> dict[str, str]:
    env = dict(environ)
    env.update(dict.fromkeys(SECRET_ENV_NAMES, ""))
    if auth.mode is AuthMode.API_KEY:
        value = environ.get(auth.api_key_env, "")
        if not value:
            raise MissingApiKeyError(
                f"{auth.api_key_env} is empty or unset, so API-key auth cannot be used"
            )
        env[auth.api_key_env] = value
    elif auth.mode is not AuthMode.OAUTH:
        raise ValueError(f"unknown auth mode: {auth.mode!r}")
    if auth.config_dir is not None:
        env["CLAUDE_CONFIG_DIR"] = str(auth.config_dir)
    return env


def describe_auth(auth: AuthConfig, environ: Mapping[str, str]) -> dict[str, object]:
    return {
        "mode": auth.mode.value,
        "config_dir": str(auth.config_dir) if auth.config_dir is not None else "default",
        "api_key_present": bool(environ.get(auth.api_key_env)),
    }
