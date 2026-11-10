"""Memory-owned service configuration, independent of agent prompt injection."""

from pydantic import BaseModel, ConfigDict, Field

from base.packages.plugins.extensions import PluginContributions


class MemoryConfig(BaseModel):
    model_config = ConfigDict(frozen=True)

    lfs_enabled: bool = Field(
        default=False,
        strict=True,
        title="Enable Git LFS for Vault",
        description="Configure Vault LFS tracking during explicit memory initialization.",
        json_schema_extra={"scope": "host", "writable": True, "remote_writable": False},
    )
    lfs_url: str = Field(
        default="",
        title="Git LFS server URL",
        description="LFS endpoint written to the Vault when LFS is enabled and this URL is nonempty.",
        json_schema_extra={"scope": "host", "writable": True, "remote_writable": False},
    )

    indexer_enabled: bool = Field(
        title="Enable memory indexing",
        default=True,
        strict=True,
        description="Run the gateway memory indexer, even when memory index injection is disabled.",
        json_schema_extra={
            "scope": "host",
            "writable": True,
            "remote_writable": False,
            "restart_required": "gateway",
        },
    )


def contribute() -> PluginContributions:
    """Declare the memory service config without loading the agent SDK."""
    return PluginContributions(config=MemoryConfig)
