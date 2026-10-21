from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from base.host.env.registry import session_child_env_keys

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
# The child environment is an allowlist, and the SDK does not let us express it as one:
# it spawns the CLI with `{**os.environ, **options.env}`, so a key this mapping omits is
# simply filled back in from the parent. Every key the host carries and the allowlist
# does not name is therefore set to the empty string here — the session sees
# `session_child_env_keys()` and nothing else (#5512: the host's own OPENAI_API_KEY rode
# into a claude session's Bash tool on a machine whose supervisor was started from a
# shell carrying `~/.meiva/.env`).
#
# "Unset" is an empty value for this CLI, which is why the two credential variables stay
# blanked rather than dropped: a caller that exports a key must not silently move a
# subscription session onto metered billing, and API-key mode restores exactly the one
# variable the caller named. Nothing here reads, stores or forwards a secret, which is
# also why `describe_auth` reports presence only.
#
# What the caller hands in still rides, because the delivery adds the session's
# registration handle (`MEIVA_HARNESS_HANDLE`) and the runner's home allowlist on top of
# the allowlist; dropping those left every shell the CLI spawned without an identity — a
# live turn read `meiva.self.AGENT_ID` as `None` inside its own Bash tool.
# meiva:link base/lm/harness/claude/docs/claude.meiva.okf.md
def build_env(
    environ: Mapping[str, str],
    auth: AuthConfig,
    *,
    host: Mapping[str, str],
) -> dict[str, str]:
    allowed = session_child_env_keys()
    env = {key: "" for key in host if key not in allowed}
    env.update({key: value for key, value in environ.items() if key in allowed})
    env.update(dict.fromkeys(SECRET_ENV_NAMES, ""))
    if auth.mode is AuthMode.API_KEY:
        value = host.get(auth.api_key_env, "")
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
