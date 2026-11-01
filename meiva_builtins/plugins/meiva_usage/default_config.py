# meiva:note
# Declarative configuration models for meiva_usage.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import re
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

from base.packages.plugins.extensions import PluginContributions
from base.packages.plugins.surface.schedules import StatCardSpec

ENV_NAME_RE = r"^[A-Z][A-Z0-9_]*$"
CARD_ID_RE = r"^[a-z0-9][a-z0-9_-]*$"

_COMPILED_ENV_NAME_RE = re.compile(ENV_NAME_RE)
_COMPILED_CARD_ID_RE = re.compile(CARD_ID_RE)

ProviderKind = Literal["claude", "codex", "antigravity", "deepseek", "openai", "anthropic"]

ANTIGRAVITY_GEMINI_SUFFIX = "-gemini"
ANTIGRAVITY_CLAUDE_GPT_SUFFIX = "-claude-gpt"


def antigravity_card_ids(collector_id: str) -> tuple[str, str]:
    return (
        f"{collector_id}{ANTIGRAVITY_GEMINI_SUFFIX}",
        f"{collector_id}{ANTIGRAVITY_CLAUDE_GPT_SUFFIX}",
    )


_COMPILED_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _validate_balance_as_of(val: str) -> None:
    if _COMPILED_DATE_RE.match(val) is None:
        raise ValueError("balance_as_of must match 'YYYY-MM-DD'")
    try:
        date.fromisoformat(val)
    except ValueError as exc:
        raise ValueError(f"balance_as_of is not a valid date: {exc}") from None


def _validate_admin_cost_baseline(collector: CollectorConfig) -> None:
    if (collector.known_balance_usd is None) != (collector.balance_as_of is None):
        raise ValueError("known_balance_usd and balance_as_of must be configured together")
    if collector.balance_as_of is not None:
        _validate_balance_as_of(collector.balance_as_of)


def _validate_env_var_name(name: str | None, field_name: str) -> None:
    if name is None:
        return
    if _COMPILED_ENV_NAME_RE.match(name) is None:
        raise ValueError(f"{field_name}: '{name}' must match {ENV_NAME_RE}")
    if name.startswith("MEIVA_"):
        raise ValueError(f"{field_name}: '{name}' must not start with 'MEIVA_'")


def _check_disallowed_fields(
    collector: CollectorConfig, disallowed: tuple[str, ...], name: str
) -> None:
    for field in disallowed:
        if getattr(collector, field) is not None:
            if field.startswith("oauth_"):
                raise ValueError(f"{name} collector does not accept OAuth client env vars")
            raise ValueError(f"{name} collector does not accept {field}")


def _validate_deepseek_selectors(collector: CollectorConfig) -> None:
    if collector.key_env is None:
        raise ValueError("DeepSeek collector requires key_env")
    _check_disallowed_fields(
        collector,
        (
            "credentials_path",
            "keychain_account",
            "account_email",
            "oauth_client_id_env",
            "oauth_client_secret_env",
            "known_balance_usd",
            "balance_as_of",
        ),
        "DeepSeek",
    )


def _validate_claude_selectors(collector: CollectorConfig) -> None:
    _check_disallowed_fields(
        collector,
        (
            "key_env",
            "account_email",
            "oauth_client_id_env",
            "oauth_client_secret_env",
            "known_balance_usd",
            "balance_as_of",
        ),
        "Claude",
    )


def _validate_codex_selectors(collector: CollectorConfig) -> None:
    _check_disallowed_fields(
        collector,
        (
            "key_env",
            "keychain_account",
            "oauth_client_id_env",
            "oauth_client_secret_env",
            "known_balance_usd",
            "balance_as_of",
        ),
        "Codex",
    )


def _validate_antigravity_selectors(collector: CollectorConfig) -> None:
    _check_disallowed_fields(
        collector,
        ("key_env", "keychain_account", "known_balance_usd", "balance_as_of"),
        "Antigravity",
    )
    has_id = collector.oauth_client_id_env is not None
    has_secret = collector.oauth_client_secret_env is not None
    if has_id != has_secret:
        raise ValueError(
            "oauth_client_id_env and oauth_client_secret_env must be configured together"
        )


def _validate_openai_selectors(collector: CollectorConfig) -> None:
    if collector.key_env is None:
        raise ValueError("OpenAI collector requires key_env")
    _check_disallowed_fields(
        collector,
        (
            "credentials_path",
            "keychain_account",
            "account_email",
            "oauth_client_id_env",
            "oauth_client_secret_env",
        ),
        "OpenAI",
    )
    _validate_admin_cost_baseline(collector)


def _validate_anthropic_selectors(collector: CollectorConfig) -> None:
    if collector.key_env is None:
        raise ValueError("Anthropic collector requires key_env")
    _check_disallowed_fields(
        collector,
        (
            "credentials_path",
            "keychain_account",
            "account_email",
            "oauth_client_id_env",
            "oauth_client_secret_env",
        ),
        "Anthropic",
    )
    _validate_admin_cost_baseline(collector)


_PROVIDER_VALIDATORS = {
    "deepseek": _validate_deepseek_selectors,
    "claude": _validate_claude_selectors,
    "codex": _validate_codex_selectors,
    "antigravity": _validate_antigravity_selectors,
    "openai": _validate_openai_selectors,
    "anthropic": _validate_anthropic_selectors,
}


def _validate_provider_selectors(collector: CollectorConfig) -> None:
    validator = _PROVIDER_VALIDATORS.get(collector.provider)
    if validator is not None:
        validator(collector)


def _validate_collector_strings(collector: CollectorConfig) -> None:
    if _COMPILED_CARD_ID_RE.match(collector.id) is None:
        raise ValueError(f"id: '{collector.id}' must match {CARD_ID_RE}")
    if not collector.label.strip() or collector.label != collector.label.strip():
        raise ValueError("label must be non-empty without leading or trailing whitespace")
    if not collector.machine.strip() or collector.machine != collector.machine.strip():
        raise ValueError("machine must be non-empty without leading or trailing whitespace")


def _validate_no_whitespace(val: str | None, name: str) -> None:
    if val is not None and (not val.strip() or any(c.isspace() for c in val)):
        raise ValueError(f"{name} must contain no whitespace")


def _validate_no_outer_whitespace(val: str | None, name: str) -> None:
    if val is not None and (not val.strip() or val != val.strip()):
        raise ValueError(f"{name} must have no outer whitespace")


def _validate_collector_optional_strings(collector: CollectorConfig) -> None:
    _validate_no_outer_whitespace(collector.credentials_path, "credentials_path")
    _validate_no_whitespace(collector.keychain_account, "keychain_account")
    _validate_no_whitespace(collector.account_email, "account_email")


class CollectorConfig(BaseModel):
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
    known_balance_usd: float | None = Field(default=None, ge=0.0)
    balance_as_of: str | None = None

    def secret_env_vars(self) -> tuple[str, ...]:
        ordered: list[str] = []
        for name in (self.key_env, self.oauth_client_id_env, self.oauth_client_secret_env):
            if name is not None and name not in ordered:
                ordered.append(name)
        return tuple(ordered)

    def stat_cards(self) -> tuple[StatCardSpec, ...]:
        if self.provider == "antigravity":
            gemini_id, claude_gpt_id = antigravity_card_ids(self.id)
            return (
                StatCardSpec(id=gemini_id, label=f"{self.label} · Gemini"),
                StatCardSpec(id=claude_gpt_id, label=f"{self.label} · Claude & GPT"),
            )
        return (StatCardSpec(id=self.id, label=self.label),)

    @model_validator(mode="after")
    def validate_collector(self) -> Self:
        _validate_collector_strings(self)
        self.stat_cards()
        _validate_env_var_name(self.key_env, "key_env")
        _validate_env_var_name(self.oauth_client_id_env, "oauth_client_id_env")
        _validate_env_var_name(self.oauth_client_secret_env, "oauth_client_secret_env")
        _validate_collector_optional_strings(self)
        _validate_provider_selectors(self)
        return self


class UsageConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    collectors: tuple[CollectorConfig, ...] = Field(
        default=(),
        json_schema_extra={"scope": "cluster-pinned", "restart_required": ""},
    )

    @model_validator(mode="after")
    def validate_collectors(self) -> Self:
        seen_collector_ids: set[str] = set()
        seen_card_ids: set[str] = set()
        for collector in self.collectors:
            if collector.id in seen_collector_ids:
                raise ValueError(f"Duplicate collector id: '{collector.id}'")
            seen_collector_ids.add(collector.id)
            for card in collector.stat_cards():
                if card.id in seen_card_ids:
                    raise ValueError(f"Duplicate stat card id: '{card.id}'")
                seen_card_ids.add(card.id)
        return self


def contribute() -> PluginContributions:
    return PluginContributions(config=UsageConfig)
