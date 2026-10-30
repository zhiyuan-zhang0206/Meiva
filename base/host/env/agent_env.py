# meiva:note
# The environment variables that carry an agent's id, config overlay and birth stamp to a child.
#
# The config ones travel as JSON in env vars, never argv (issue #974: argv is world-readable via `ps`). They
# are not Settings fields: they are process-bound carriers the exec launcher writes and the child
# boot consumes.

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields
from typing import Any

from base.host.env.config_lite_table import FIELD_ALIASES, FIELD_SCOPES

__all__ = [
    "AGENT_BIRTH_CONFIG_ENV",
    "AGENT_CONFIG_OVERLAY_ENV",
    "AGENT_ID_ENV",
    "HOST_PASSTHROUGH_KEYS",
    "MEIVA_HOME_ENV",
    "NETWORK_PROXY_KEYS",
    "SESSION_CREDENTIAL_KEYS",
    "ModelOverrides",
    "session_child_env_keys",
]

AGENT_ID_ENV = "MEIVA_AGENT_ID"
AGENT_CONFIG_OVERLAY_ENV = "MEIVA_AGENT_CONFIG_OVERLAY"
AGENT_BIRTH_CONFIG_ENV = "MEIVA_AGENT_BIRTH_CONFIG"
MEIVA_HOME_ENV = "MEIVA_HOME"

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
_TEMP_DIR_KEYS = frozenset({"TMPDIR", "TEMP", "TMP"})
_SESSION_DELIVERY_KEYS = frozenset({"PATH", "MEIVA_HARNESS_HANDLE", "CLAUDE_CONFIG_DIR"})

# meiva:note
# Credentials a session child never inherits, whatever its parent happens to carry (task #5864,
# 405 ruling 2026-10-30).
#
# A pty session's environment is the pty-sessions service's own environment plus the caller's
# overlay, so every secret the service inherits reaches every shell child - a schedule script, an
# agent shell, a watcher. Measured 2026-10-30 on the gateway home: a schedule script saw 107
# MEIVA_* keys including the cluster secret, the Cloudflare token, the Feishu app secret and the
# redis passwords, none of which any schedule job reads.
#
# Rule: a credential is read from the file that holds it, never carried in a child's environment.
# A job that needs one reads its home's `.env` or its installed capability; `consume_unit` already
# supplies the machine token that way, and `schedules/jobs/self_evolution_weekly.py` reads the
# cluster secret out of the `.env` text itself.
#
# `MEIVA_DB_URL` and `MEIVA_REDIS_URL` joined this set in the second step (task #5866). They carry
# the unit's database password and the cache password - the highest-value pair of the batch - so
# they cannot stay resident in every session's environment. The diagnosis channel they used to
# provide (`psql "$MEIVA_DB_URL"`) moves to `scripts/host_ops/run_with_env.py`, which reads each
# value fresh and pins it into one child's environment.
#
# What an agent-profile child loses is the inherited copy only: `dotenv_boot` re-reads
# `MEIVA_DB_URL` from the home's `.env` during `import meiva`, so a process that boots the platform
# still gets its login (measured 2026-10-30: `import meiva` with `MEIVA_PROCESS_PROFILE=agent` and
# both keys removed prints `IMPORT_OK` and the right AGENT_ID). A plain shell tool is what stops
# working, which is exactly what run_with_env.py restores.
SESSION_CREDENTIAL_KEYS = frozenset(
    {
        "MEIVA_ALERTS_WEBHOOK_TOKEN",
        "MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE",
        "MEIVA_CF_API_TOKEN",
        "MEIVA_CLUSTER_SECRET",
        "MEIVA_DB_URL",
        "MEIVA_FEISHU_APP_SECRET",
        "MEIVA_REDIS_ADMIN_PASSWORD",
        "MEIVA_REDIS_PASSWORD",
        "MEIVA_REDIS_URL",
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
        raw: Mapping[str, Any] = pins or {}
        return cls(**{f.name: raw.get(f.name) for f in fields(cls)})
