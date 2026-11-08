"""Services config — ServiceSettings.

Split out of the former flat Settings god object; each field keeps its exact
env alias so the .env surface is unchanged. Browser, helper, and memory-service
fields are inherited from `service_runtime`; aggregated by base/config.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import Field

from base.config.base import unit_home
from base.config.domains.services.health_ports_fields import ServiceHealthPortFields
from base.config.domains.services.runtime import ServiceRuntimeSettings


class ServiceSettings(ServiceHealthPortFields, ServiceRuntimeSettings):
    labeler_max_chars: int = Field(
        title="Agent label maximum length (characters)",
        default=64,
        gt=0,
        alias="MEIVA_LABELER_MAX_CHARS",
        description=(
            "Character ceiling for an auto-generated agent label: the model is "
            "asked for a label of at most this many characters, and the output is "
            "truncated to the same number. 64 is a readable single line in the "
            "fleet view — long enough to distinguish one task from another, short "
            "enough that labels scan side by side."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    backup_hour: int = Field(
        title="Daily backup hour (cluster time)",
        default=3,
        ge=0,
        le=23,
        alias="MEIVA_BACKUP_HOUR",
        description=(
            "Cluster-clock hour (0-23) at which the daily logical dump becomes due: "
            "the scheduler's first wake at/after this hour starts it, so a host that "
            "was down at that hour catches up on its next tick. 03:00 runs in the "
            "quiet window, and the newest dump is then only hours old when the "
            "morning reads it; the hour is read on MEIVA_TIMEZONE (cluster time), "
            "never the host clock."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    backup_keep: int = Field(
        title="Daily backups to retain",
        default=7,
        gt=0,
        alias="MEIVA_BACKUP_KEEP",
        description=(
            "How many of the newest daily dumps the local pool keeps. 7 is a week "
            "of dailies: a bad migration found a day later must not have already "
            "overwritten the last good copy."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )
    backup_offsite_endpoint: str = Field(
        title="Off-site backup storage endpoint",
        default="",
        alias="MEIVA_BACKUP_OFFSITE_ENDPOINT",
        description=(
            "Aliyun OSS region endpoint (e.g. https://oss-cn-shanghai.aliyuncs.com) "
            "the daily dump is published to. The off-site leg is skipped, with one "
            "log line, until the endpoint, the bucket and the credentials file are "
            "all set."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    backup_offsite_bucket: str = Field(
        title="Off-site backup storage bucket",
        default="",
        alias="MEIVA_BACKUP_OFFSITE_BUCKET",
        description="Aliyun OSS bucket the daily dump is published to (under `meiva-logical/`).",
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
            "bootstrap": False,
        },
    )
    backup_offsite_credentials_file: Path | None = Field(
        title="Off-site backup credentials file",
        default=None,
        alias="MEIVA_BACKUP_OFFSITE_CREDENTIALS_FILE",
        description=(
            "0600 JSON holding the Aliyun OSS RAM AccessKey pair "
            "(access_key_id, access_key_secret) the off-site publish uses. "
            "Never the cluster secret."
        ),
        json_schema_extra={
            "restart_required": "gateway",
            "writable": True,
            "sensitive": True,
            "scope": "host",
            "remote_writable": False,
            "bootstrap": False,
        },
    )

    gateway_pidfile: Path = Field(
        title="Gateway process ID file",
        default_factory=lambda: unit_home() / "run" / "gateway.pid",
        alias="MEIVA_GATEWAY_PIDFILE",
        description="Gateway uvicorn pidfile path. healthcheck goes via HTTP; this is auxiliary only.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    frontend_healthcheck_url: str = Field(
        title="Frontend health check URL",
        default="http://localhost:3000",
        alias="MEIVA_FRONTEND_HEALTHCHECK_URL",
        description="The fleet UI entry the user reaches — Gate's port (the Next.js app itself binds MEIVA_APP_PORT and is proxied).",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    app_port: int | None = Field(
        title="Web application port",
        default=None,
        alias="MEIVA_APP_PORT",
        description="The Next.js app port the gate proxies to (the gate owns the entry port). Unset = entry port + 1; converge writes the explicit value from the cluster record.",
        json_schema_extra={
            "help_text": "Leave unset to derive the application port from the entry port.",
            "restart_required": "all",
            "writable": True,
            "sensitive": False,
            "scope": "cluster-pinned",
        },
    )

    gateway_health_url: str = Field(
        title="Gateway health check URL",
        default="http://localhost:8000/api/health",
        alias="MEIVA_GATEWAY_HEALTH_URL",
        description="Gateway healthcheck probe URL. Pure agent-runners derive it from MEIVA_GATEWAY_URL when no host override is set; gateway-capable units default to the local gateway.",
        json_schema_extra={
            "restart_required": "",
            "writable": False,
            "sensitive": False,
            "scope": "host",
            "remote_writable": False,
        },
    )

    ops_concurrency: int = Field(
        title="Maximum concurrent cluster operations",
        default=8,
        alias="MEIVA_OPS_CONCURRENCY",
        description="Max concurrent cluster ops the agent-runner executes; further inbound /ops requests queue. Prevents one fan-out from overwhelming the runner.",
        json_schema_extra={
            "capability": "agent-runner",
            "restart_required": "ops",
            "writable": True,
            "sensitive": False,
            "scope": "host",
            "remote_writable": True,
        },
    )
