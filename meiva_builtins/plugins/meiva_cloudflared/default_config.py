# meiva:note
# Typed configuration schema for the meiva_cloudflared keepalive plugin.

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.extensions import PluginContributions


class CloudflaredConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cloudflared_enabled: bool = Field(
        default=True,
        description="Enable the cloudflared tunnel keepalive service when a cloudflared tunnel is configured on the host.",
    )
    cloudflared_bin: str = Field(
        default="cloudflared",
        min_length=1,
        description="Executable name or absolute path for cloudflared.",
    )
    cloudflared_config_path: str = Field(
        default="",
        description="Optional path to cloudflared config.yml. Defaults to ~/.cloudflared/config.yml or /etc/cloudflared/config.yml when present.",
    )
    cloudflared_tunnel: str = Field(
        default="",
        description="Optional tunnel name or UUID passed to 'cloudflared tunnel run'. When empty, cloudflared uses the tunnel from its config file.",
    )
    cloudflared_extra_args: list[str] = Field(
        default_factory=list,
        description="Extra CLI arguments inserted before 'run'.",
    )
    cloudflared_check_interval_seconds: float = Field(
        default=15.0,
        ge=2.0,
        le=300.0,
        description="Seconds between cloudflared liveness checks.",
    )


def contribute() -> PluginContributions:
    return PluginContributions(config=CloudflaredConfig)
