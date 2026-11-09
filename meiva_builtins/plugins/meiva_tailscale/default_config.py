# meiva:note
# Typed configuration schema for the meiva_tailscale keepalive plugin.

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.extensions import PluginContributions


class TailscaleConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    tailscale_enabled: bool = Field(
        default=True,
        description="Enable the Tailscale keepalive watchdog service when Tailscale is installed on the host.",
    )
    tailscale_bin: str = Field(
        default="tailscale",
        min_length=1,
        description="Executable name or absolute path for the tailscale CLI.",
    )
    tailscale_auto_up: bool = Field(
        default=True,
        description="Automatically run 'tailscale up' when BackendState is Stopped or disconnected.",
    )
    tailscale_up_args: list[str] = Field(
        default_factory=list,
        description="Extra CLI arguments passed to 'tailscale up'.",
    )
    tailscale_check_interval_seconds: float = Field(
        default=30.0,
        ge=5.0,
        le=600.0,
        description="Seconds between Tailscale status checks.",
    )


def contribute() -> PluginContributions:
    return PluginContributions(config=TailscaleConfig)
