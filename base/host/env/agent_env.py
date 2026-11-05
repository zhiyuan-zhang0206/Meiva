# meiva:note
# The environment variables that carry an agent's id, config overlay and birth stamp to a child.
#
# The config ones travel as JSON in env vars, never argv (issue #974: argv is world-readable via `ps`). They
# are not Settings fields: they are process-bound carriers the exec launcher writes and the child
# boot consumes.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from pathlib import Path

from base.host.env.config_lite_table import FIELD_ALIASES, FIELD_SCOPES

__all__ = [
    "AGENT_BIRTH_CONFIG_ENV",
    "AGENT_CONFIG_OVERLAY_ENV",
    "AGENT_ID_ENV",
    "HOST_PASSTHROUGH_KEYS",
    "MEIVA_HOME_ENV",
    "MEIVA_PYTHON_ENV",
    "NETWORK_PROXY_KEYS",
    "SESSION_CREDENTIAL_KEYS",
    "TEMP_DIR_KEYS",
    "ModelOverrides",
    "built_from_values",
    "session_child_env_keys",
    "temp_dir_of",
]

AGENT_ID_ENV = "MEIVA_AGENT_ID"
AGENT_CONFIG_OVERLAY_ENV = "MEIVA_AGENT_CONFIG_OVERLAY"
AGENT_BIRTH_CONFIG_ENV = "MEIVA_AGENT_BIRTH_CONFIG"
MEIVA_HOME_ENV = "MEIVA_HOME"
MEIVA_PYTHON_ENV = "MEIVA_PYTHON"

HOST_PASSTHROUGH_KEYS = frozenset({"DISPLAY", "WAYLAND_DISPLAY", "HOME", "USER", "LOGNAME"})
NETWORK_PROXY_KEYS = frozenset(
    {
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    }
)
# meiva:note
# The keys that name a temp root, in the order a POSIX tool reads them. The same order is why
# `child_env` copies all three non-empty into every child dict: a caller holding that child mapping
# can then name the directory its child will use instead of asking `tempfile` for this process's
# answer.
TEMP_DIR_KEYS: tuple[str, ...] = ("TMPDIR", "TEMP", "TMP")
_TEMP_DIR_KEYS = frozenset(TEMP_DIR_KEYS)


# meiva:note
# The temp root a given environment names, and /tmp when it names none: that last case is the POSIX
# fallback every tool reads when no key is set, so it is a constant here rather than a read of this
# process's own environment.
def temp_dir_of(environ: Mapping[str, str]) -> Path:
    for key in TEMP_DIR_KEYS:
        value = environ.get(key)
        if value:
            return Path(value)
    return Path("/tmp")  # noqa: S108


_SESSION_DELIVERY_KEYS = frozenset(
    {"PATH", MEIVA_PYTHON_ENV, "MEIVA_HARNESS_HANDLE", "CLAUDE_CONFIG_DIR"}
)

# meiva:note
# Credentials a session child never inherits, whatever its parent happens to carry (task #5864,
# 405 ruling 2026-10-30).
#
# A pty session's environment is the pty-sessions service's own environment plus the caller's
# overlay, so every secret the service inherits reaches every shell child - a schedule script, an
# agent shell, a watcher. Measured 2026-10-30 on the gateway home: a schedule script saw 107
# MEIVA_* keys including the cluster secret, the Cloudflare token, retired channel secrets and the
# redis passwords, none of which any schedule job reads.
#
# Rule: a credential is read from the file that holds it, never carried in a child's environment.
# A job that needs one reads its home's `.env` or its installed capability; `consume_unit` already
# supplies the machine token that way, and `schedules/jobs/self_evolution_weekly.py` reads the
# cluster secret out of the `.env` text itself.
#
# `MEIVA_DB_URL` and `MEIVA_REDIS_URL` are deliberately NOT here: they also carry a password, but
# they are the operator's psql/redis-cli channel for a live session, and stripping them would
# break diagnosis on every host. They are the first candidates of the second step.
SESSION_CREDENTIAL_KEYS = frozenset(
    {
        "MEIVA_ALERTS_WEBHOOK_TOKEN",
        "MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE",
        "MEIVA_CF_API_TOKEN",
        "MEIVA_CLUSTER_SECRET",
        "MEIVA_FEISHU_APP_SECRET",
        "MEIVA_REDIS_ADMIN_PASSWORD",
        "MEIVA_REDIS_PASSWORD",
        "MEIVA_TELEGRAM_BOT_TOKEN",
    }
)


def session_child_env_keys() -> frozenset[str]:
    host_aliases = frozenset(
        FIELD_ALIASES[name] for name, scope in FIELD_SCOPES.items() if scope == "host"
    )
    return (
        host_aliases
        | {MEIVA_HOME_ENV}
        | HOST_PASSTHROUGH_KEYS
        | _TEMP_DIR_KEYS
        | NETWORK_PROXY_KEYS
        | _SESSION_DELIVERY_KEYS
    )


# meiva:note
# A frozen dataclass of `slice_type` holding `values` by field name. The values are `object` (a pin
# or a live default, already validated by the settings that produced it), and a constructor call
# cannot take them without naming `Any`, so the fields are set directly; like the constructor call
# it replaces, this does not re-validate them. The caller names every field.
def built_from_values[S](slice_type: type[S], values: Mapping[str, object]) -> S:
    built = object.__new__(slice_type)
    for name, value in values.items():
        object.__setattr__(built, name, value)
    return built


@dataclass(frozen=True)
class ModelOverrides:
    llm_stream_ttft_timeout_seconds: float | None
    llm_stream_total_timeout_seconds: float | None
    llm_stream_inter_chunk_timeout_seconds: float | None
    reasoning_effort: str | None
    claude_thinking_budget_tokens: int | None
    auto_compact_fraction: float | None
    auto_compact_ceiling_tokens: int | None
    compact_reminder_fraction: float | None

    @classmethod
    def from_pins(cls, pins: Mapping[str, object] | None) -> ModelOverrides:
        raw = pins or {}
        return built_from_values(cls, {f.name: raw.get(f.name) for f in fields(cls)})
