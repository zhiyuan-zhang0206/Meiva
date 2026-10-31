"""Unit tests for meiva_usage configuration models."""

from __future__ import annotations

import pytest

from meiva_builtins.plugins.meiva_usage.default_config import (
    CollectorConfig,
    UsageConfig,
)
from meiva_builtins.plugins.meiva_usage.default_config import (
    contribute as contribute_default_config,
)
from meiva_builtins.plugins.meiva_usage.plugin import contribute as contribute_plugin


def test_valid_multi_account_config() -> None:
    c1 = CollectorConfig(
        id="deepseek-primary",
        label="DeepSeek Primary",
        provider="deepseek",
        machine="host-a",
        key_env="DEEPSEEK_API_KEY",
    )
    c2 = CollectorConfig(
        id="deepseek-secondary",
        label="DeepSeek Secondary",
        provider="deepseek",
        machine="host-a",
        key_env="DEEPSEEK_API_KEY_2",
    )
    c3 = CollectorConfig(
        id="claude-personal",
        label="Claude Personal",
        provider="claude",
        machine="host-b",
        keychain_account="user@example.com",
    )
    c4 = CollectorConfig(
        id="claude-work",
        label="Claude Work",
        provider="claude",
        machine="host-b",
        credentials_path="~/.claude/work.json",
    )
    c5 = CollectorConfig(
        id="codex-default",
        label="Codex Default",
        provider="codex",
        machine="host-c",
        credentials_path="~/.codex-work",
    )
    c6 = CollectorConfig(
        id="codex-team",
        label="Codex Team",
        provider="codex",
        machine="host-c",
        account_email="team@example.com",
    )
    c7 = CollectorConfig(
        id="antigravity-static",
        label="Antigravity Static",
        provider="antigravity",
        machine="host-d",
        account_email="dev@example.com",
    )
    c8 = CollectorConfig(
        id="antigravity-refreshed",
        label="Antigravity Refreshed",
        provider="antigravity",
        machine="host-d",
        oauth_client_id_env="ANTIGRAVITY_CLIENT_ID",
        oauth_client_secret_env="ANTIGRAVITY_CLIENT_SECRET",  # noqa: S106
    )

    config = UsageConfig(collectors=(c1, c2, c3, c4, c5, c6, c7, c8))
    assert len(config.collectors) == 8


def test_duplicate_collector_id_rejected() -> None:
    c1 = CollectorConfig(
        id="shared-id",
        label="Collector 1",
        provider="deepseek",
        machine="host-a",
        key_env="KEY_A",
    )
    c2 = CollectorConfig(
        id="shared-id",
        label="Collector 2",
        provider="deepseek",
        machine="host-a",
        key_env="KEY_B",
    )
    with pytest.raises(ValueError, match="Duplicate collector id"):
        UsageConfig(collectors=(c1, c2))


def test_invalid_card_id_rejected() -> None:
    for invalid_id in ("UPPER_CASE", "-leading-dash", "_underscore", "space id", "special@id"):
        with pytest.raises(ValueError, match="must match"):
            CollectorConfig(
                id=invalid_id,
                label="Valid Label",
                provider="deepseek",
                machine="host-a",
                key_env="KEY_A",
            )


def test_meiva_prefixed_env_rejected() -> None:
    for forbidden in ("MEIVA_KEY", "MEIVA_SECRET", "MEIVA_TOKEN"):
        with pytest.raises(ValueError, match="must not start with 'MEIVA_'"):
            CollectorConfig(
                id="test-card",
                label="Test",
                provider="deepseek",
                machine="host-a",
                key_env=forbidden,
            )


def test_invalid_env_name_format_rejected() -> None:
    for invalid_env in ("lower_case", "123_START", "WITH-DASHES", "WITH SPACES"):
        with pytest.raises(ValueError, match="must match"):
            CollectorConfig(
                id="test-card",
                label="Test",
                provider="deepseek",
                machine="host-a",
                key_env=invalid_env,
            )


def test_unpaired_oauth_client_env_rejected() -> None:
    with pytest.raises(ValueError, match="configured together"):
        CollectorConfig(
            id="ag-unpaired-id",
            label="AG Unpaired ID",
            provider="antigravity",
            machine="host-a",
            oauth_client_id_env="MY_CLIENT_ID",
        )

    with pytest.raises(ValueError, match="configured together"):
        CollectorConfig(
            id="ag-unpaired-sec",
            label="AG Unpaired Secret",
            provider="antigravity",
            machine="host-a",
            oauth_client_secret_env="MY_CLIENT_SECRET",  # noqa: S106
        )


def test_deepseek_requires_key_env() -> None:
    with pytest.raises(ValueError, match="DeepSeek collector requires key_env"):
        CollectorConfig(
            id="deepseek-missing-key",
            label="DeepSeek Missing Key",
            provider="deepseek",
            machine="host-a",
        )


def test_provider_mismatched_selectors_rejected() -> None:
    # key_env on non-deepseek
    with pytest.raises(ValueError, match="Claude collector does not accept key_env"):
        CollectorConfig(
            id="claude-bad",
            label="Claude",
            provider="claude",
            machine="host-a",
            key_env="SOME_KEY",
        )

    # keychain_account on non-claude
    with pytest.raises(ValueError, match="Codex collector does not accept keychain_account"):
        CollectorConfig(
            id="codex-bad",
            label="Codex",
            provider="codex",
            machine="host-a",
            keychain_account="user@example.com",
        )

    # account_email on claude
    with pytest.raises(ValueError, match="Claude collector does not accept account_email"):
        CollectorConfig(
            id="claude-bad-email",
            label="Claude",
            provider="claude",
            machine="host-a",
            account_email="user@example.com",
        )

    # oauth_client_id_env on codex
    with pytest.raises(ValueError, match="Codex collector does not accept OAuth client env vars"):
        CollectorConfig(
            id="codex-bad-oauth",
            label="Codex",
            provider="codex",
            machine="host-a",
            oauth_client_id_env="ID_ENV",
            oauth_client_secret_env="SEC_ENV",  # noqa: S106
        )

    # credentials_path on deepseek
    with pytest.raises(ValueError, match="DeepSeek collector does not accept credentials_path"):
        CollectorConfig(
            id="deepseek-bad-path",
            label="DeepSeek",
            provider="deepseek",
            machine="host-a",
            key_env="DEEPSEEK_KEY",
            credentials_path="~/.deepseek/creds.json",
        )


def test_secret_env_vars_deduplication() -> None:
    c1 = CollectorConfig(
        id="c1",
        label="C1",
        provider="antigravity",
        machine="host-a",
        oauth_client_id_env="SAME_VAR",
        oauth_client_secret_env="SAME_VAR",  # noqa: S106
    )
    assert c1.secret_env_vars() == ("SAME_VAR",)

    c2 = CollectorConfig(
        id="c2",
        label="C2",
        provider="deepseek",
        machine="host-a",
        key_env="MY_KEY",
    )
    assert c2.secret_env_vars() == ("MY_KEY",)


def test_contribute_returns_contributions() -> None:
    contrib_default = contribute_default_config()
    assert contrib_default.config is UsageConfig

    contrib_plugin = contribute_plugin()
    assert contrib_plugin.config is None
