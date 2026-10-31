"""Declarative configuration models for meiva_usage."""

from __future__ import annotations

import re
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from base.packages.plugins.extensions import PluginContributions

ENV_NAME_RE = r"^[A-Z][A-Z0-9_]*$"
CARD_ID_RE = r"^[a-z0-9][a-z0-9_-]*$"

_COMPILED_ENV_NAME_RE = re.compile(ENV_NAME_RE)
_COMPILED_CARD_ID_RE = re.compile(CARD_ID_RE)

ProviderKind = Literal["claude", "codex", "antigravity", "deepseek"]


def _validate_env_var_name(name: str | None, field_name: str) -> None:
    if name is None:
        return
    if _COMPILED_ENV_NAME_RE.match(name) is None:
        raise ValueError(f"{field_name}: '{name}' must match {ENV_NAME_RE}")
    if name.startswith("MEIVA_"):
        raise ValueError(f"{field_name}: '{name}' must not start with 'MEIVA_'")


def _validate_provider_selectors(collector: CollectorConfig) -> None:
    provider = collector.provider
    if provider == "deepseek":
        if collector.key_env is None:
            raise ValueError("DeepSeek collector requires key_env")
        if collector.credentials_path is not None:
            raise ValueError("DeepSeek collector does not accept credentials_path")
        if collector.keychain_account is not None:
            raise ValueError("DeepSeek collector does not accept keychain_account")
        if collector.account_email is not None:
            raise ValueError("DeepSeek collector does not accept account_email")
        if (
            collector.oauth_client_id_env is not None
            or collector.oauth_client_secret_env is not None
        ):
            raise ValueError("DeepSeek collector does not accept OAuth client env vars")

    elif provider == "claude":
        if collector.key_env is not None:
            raise ValueError("Claude collector does not accept key_env")
        if collector.account_email is not None:
            raise ValueError("Claude collector does not accept account_email")
        if (
            collector.oauth_client_id_env is not None
            or collector.oauth_client_secret_env is not None
        ):
            raise ValueError("Claude collector does not accept OAuth client env vars")

    elif provider == "codex":
        if collector.key_env is not None:
            raise ValueError("Codex collector does not accept key_env")
        if collector.keychain_account is not None:
            raise ValueError("Codex collector does not accept keychain_account")
        if (
            collector.oauth_client_id_env is not None
            or collector.oauth_client_secret_env is not None
        ):
            raise ValueError("Codex collector does not accept OAuth client env vars")

    elif provider == "antigravity":
        if collector.key_env is not None:
            raise ValueError("Antigravity collector does not accept key_env")
        if collector.keychain_account is not None:
            raise ValueError("Antigravity collector does not accept keychain_account")
        has_id = collector.oauth_client_id_env is not None
        has_secret = collector.oauth_client_secret_env is not None
        if has_id != has_secret:
            raise ValueError(
                "oauth_client_id_env and oauth_client_secret_env must be configured together"
            )


class CollectorConfig(BaseModel):
    """Configuration for one usage metrics collector."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    id: str = Field(max_length=64)
    label: str = Field(min_length=1, max_length=80)
    provider: ProviderKind
    machine: str = Field(min_length=1)
    interval_minutes: int = Field(default=10, ge=1, le=1440)
    enabled: bool = True
    key_env: str | None = None
    credentials_path: str | None = None
    keychain_account: str | None = None
    account_email: str | None = None
    oauth_client_id_env: str | None = None
    oauth_client_secret_env: str | None = None

    def secret_env_vars(self) -> tuple[str, ...]:
        """Return deduplicated configured environment variable names."""
        ordered: list[str] = []
        for name in (self.key_env, self.oauth_client_id_env, self.oauth_client_secret_env):
            if name is not None and name not in ordered:
                ordered.append(name)
        return tuple(ordered)

    @model_validator(mode="after")
    def validate_collector(self) -> Self:
        if _COMPILED_CARD_ID_RE.match(self.id) is None:
            raise ValueError(f"id: '{self.id}' must match {CARD_ID_RE}")

        if not self.label.strip() or self.label != self.label.strip():
            raise ValueError("label must be non-empty without leading or trailing whitespace")

        if not self.machine.strip() or self.machine != self.machine.strip():
            raise ValueError("machine must be non-empty without leading or trailing whitespace")

        _validate_env_var_name(self.key_env, "key_env")
        _validate_env_var_name(self.oauth_client_id_env, "oauth_client_id_env")
        _validate_env_var_name(self.oauth_client_secret_env, "oauth_client_secret_env")

        if self.credentials_path is not None and (
            not self.credentials_path.strip()
            or self.credentials_path != self.credentials_path.strip()
        ):
            raise ValueError("credentials_path must have no outer whitespace")

        if self.keychain_account is not None and (
            not self.keychain_account.strip() or any(c.isspace() for c in self.keychain_account)
        ):
            raise ValueError("keychain_account must contain no whitespace")

        if self.account_email is not None and (
            not self.account_email.strip() or any(c.isspace() for c in self.account_email)
        ):
            raise ValueError("account_email must contain no whitespace")

        _validate_provider_selectors(self)
        return self


class UsageConfig(BaseModel):
    """Root configuration for meiva_usage plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    collectors: tuple[CollectorConfig, ...] = Field(
        default=(),
        json_schema_extra={"scope": "cluster-pinned", "restart_required": ""},
    )

    @model_validator(mode="after")
    def validate_collectors(self) -> Self:
        seen: set[str] = set()
        for collector in self.collectors:
            if collector.id in seen:
                raise ValueError(f"Duplicate collector id: '{collector.id}'")
            seen.add(collector.id)
        return self


def contribute() -> PluginContributions:
    """Return plugin configuration contributions."""
    return PluginContributions(config=UsageConfig)
