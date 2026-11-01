from __future__ import annotations

from typing import Any, cast

import pytest
from pydantic import BaseModel

from base.packages.plugins.surface.schedules import StatCardSpec
from meiva_builtins.plugins.meiva_usage import ui_stats
from meiva_builtins.plugins.meiva_usage.default_config import (
    ANTIGRAVITY_CLAUDE_GPT_SUFFIX,
    ANTIGRAVITY_GEMINI_SUFFIX,
    CollectorConfig,
    UsageConfig,
    antigravity_card_ids,
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
    ag_sec_env = "ANTIGRAVITY_CLIENT_SECRET"
    c8 = CollectorConfig(
        id="antigravity-refreshed",
        label="Antigravity Refreshed",
        provider="antigravity",
        machine="host-d",
        oauth_client_id_env="ANTIGRAVITY_CLIENT_ID",
        oauth_client_secret_env=ag_sec_env,
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

    unpaired_sec = "MY_CLIENT_SECRET"
    with pytest.raises(ValueError, match="configured together"):
        CollectorConfig(
            id="ag-unpaired-sec",
            label="AG Unpaired Secret",
            provider="antigravity",
            machine="host-a",
            oauth_client_secret_env=unpaired_sec,
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
    with pytest.raises(ValueError, match="Claude collector does not accept key_env"):
        CollectorConfig(
            id="claude-bad",
            label="Claude",
            provider="claude",
            machine="host-a",
            key_env="SOME_KEY",
        )

    with pytest.raises(ValueError, match="Codex collector does not accept keychain_account"):
        CollectorConfig(
            id="codex-bad",
            label="Codex",
            provider="codex",
            machine="host-a",
            keychain_account="user@example.com",
        )

    with pytest.raises(ValueError, match="Claude collector does not accept account_email"):
        CollectorConfig(
            id="claude-bad-email",
            label="Claude",
            provider="claude",
            machine="host-a",
            account_email="user@example.com",
        )

    codex_sec = "SEC_ENV"
    with pytest.raises(ValueError, match="Codex collector does not accept OAuth client env vars"):
        CollectorConfig(
            id="codex-bad-oauth",
            label="Codex",
            provider="codex",
            machine="host-a",
            oauth_client_id_env="ID_ENV",
            oauth_client_secret_env=codex_sec,
        )

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
    same_var = "SAME_VAR"
    c1 = CollectorConfig(
        id="c1",
        label="C1",
        provider="antigravity",
        machine="host-a",
        oauth_client_id_env=same_var,
        oauth_client_secret_env=same_var,
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
    assert contrib_default.config is not None
    assert contrib_default.config.__name__ == UsageConfig.__name__
    assert issubclass(contrib_default.config, BaseModel)

    contrib_plugin = contribute_plugin()
    assert contrib_plugin.config is None


def test_openai_and_anthropic_valid_config() -> None:
    c_oai = CollectorConfig(
        id="openai-main",
        label="OpenAI Costs",
        provider="openai",
        machine="host-a",
        key_env="OPENAI_ADMIN_KEY",
    )
    assert c_oai.secret_env_vars() == ("OPENAI_ADMIN_KEY",)

    c_ant = CollectorConfig(
        id="anthropic-main",
        label="Anthropic Costs",
        provider="anthropic",
        machine="host-a",
        key_env="ANTHROPIC_ADMIN_KEY",
        known_balance_usd=50.0,
        balance_as_of="2026-10-01",
    )
    assert c_ant.secret_env_vars() == ("ANTHROPIC_ADMIN_KEY",)
    assert c_ant.known_balance_usd == 50.0
    assert c_ant.balance_as_of == "2026-10-01"


def test_openai_and_anthropic_require_key_env() -> None:
    with pytest.raises(ValueError, match="OpenAI collector requires key_env"):
        CollectorConfig(
            id="openai-no-key",
            label="OpenAI",
            provider="openai",
            machine="host-a",
        )

    with pytest.raises(ValueError, match="Anthropic collector requires key_env"):
        CollectorConfig(
            id="anthropic-no-key",
            label="Anthropic",
            provider="anthropic",
            machine="host-a",
        )


def test_openai_and_anthropic_disallow_other_selectors() -> None:
    disallowed_cases = (
        ("credentials_path", "~/.creds.json"),
        ("keychain_account", "user@example.com"),
        ("account_email", "admin@company.com"),
        ("oauth_client_id_env", "OAUTH_ID"),
        ("oauth_client_secret_env", "OAUTH_SECRET"),
    )
    for provider in ("openai", "anthropic"):
        for field, value in disallowed_cases:
            kwargs: dict[str, object] = {
                "id": f"{provider}-bad",
                "label": "Test",
                "provider": provider,
                "machine": "host-a",
                "key_env": "API_KEY",
                field: value,
            }
            with pytest.raises(ValueError):
                CollectorConfig(**cast(Any, kwargs))


def test_known_balance_and_balance_as_of_coupling() -> None:
    with pytest.raises(
        ValueError, match="known_balance_usd and balance_as_of must be configured together"
    ):
        CollectorConfig(
            id="oai-only-bal",
            label="OpenAI",
            provider="openai",
            machine="host-a",
            key_env="OPENAI_KEY",
            known_balance_usd=100.0,
        )

    with pytest.raises(
        ValueError, match="known_balance_usd and balance_as_of must be configured together"
    ):
        CollectorConfig(
            id="oai-only-date",
            label="OpenAI",
            provider="openai",
            machine="host-a",
            key_env="OPENAI_KEY",
            balance_as_of="2026-10-01",
        )


def test_balance_as_of_format_validation() -> None:
    invalid_dates = (
        "2026-10-1",
        "2026/10/01",
        "2026-13-01",
        "2026-02-30",
        " 2026-10-01 ",
        "2026-10-01T00:00:00Z",
        "not-a-date",
    )
    for bad_date in invalid_dates:
        with pytest.raises(ValueError):
            CollectorConfig(
                id="oai-bad-date",
                label="OpenAI",
                provider="openai",
                machine="host-a",
                key_env="OPENAI_KEY",
                known_balance_usd=100.0,
                balance_as_of=bad_date,
            )


def test_known_balance_disallowed_on_other_providers() -> None:
    for provider, extra in (
        ("deepseek", {"key_env": "KEY"}),
        ("claude", {"credentials_path": "~/c.json"}),
        ("codex", {"credentials_path": "~/c"}),
        ("antigravity", {"account_email": "a@b.com"}),
    ):
        kwargs1: dict[str, object] = {
            "id": f"{provider}-bal",
            "label": "Test",
            "provider": provider,
            "machine": "host-a",
            "known_balance_usd": 100.0,
            **extra,
        }
        with pytest.raises(ValueError, match="does not accept known_balance_usd"):
            CollectorConfig(**cast(Any, kwargs1))

        kwargs2: dict[str, object] = {
            "id": f"{provider}-date",
            "label": "Test",
            "provider": provider,
            "machine": "host-a",
            "balance_as_of": "2026-10-01",
            **extra,
        }
        with pytest.raises(ValueError, match="does not accept balance_as_of"):
            CollectorConfig(**cast(Any, kwargs2))


def test_known_balance_int_and_float_accepted_under_strict() -> None:
    c_int = CollectorConfig(
        id="oai-int",
        label="OpenAI Int",
        provider="openai",
        machine="host-a",
        key_env="OPENAI_KEY",
        known_balance_usd=100,
        balance_as_of="2026-10-01",
    )
    assert c_int.known_balance_usd == 100.0

    c_float = CollectorConfig(
        id="oai-float",
        label="OpenAI Float",
        provider="openai",
        machine="host-a",
        key_env="OPENAI_KEY",
        known_balance_usd=100.5,
        balance_as_of="2026-10-01",
    )
    assert c_float.known_balance_usd == 100.5

    with pytest.raises(ValueError):
        CollectorConfig(
            id="oai-neg",
            label="OpenAI Neg",
            provider="openai",
            machine="host-a",
            key_env="OPENAI_KEY",
            known_balance_usd=-5.0,
            balance_as_of="2026-10-01",
        )


def test_antigravity_stat_cards_generation() -> None:
    assert ANTIGRAVITY_GEMINI_SUFFIX == "-gemini"
    assert ANTIGRAVITY_CLAUDE_GPT_SUFFIX == "-claude-gpt"
    assert antigravity_card_ids("my-ag") == ("my-ag-gemini", "my-ag-claude-gpt")

    ag = CollectorConfig(
        id="ag-main",
        label="AG Main",
        provider="antigravity",
        machine="host-a",
    )
    cards = ag.stat_cards()
    assert len(cards) == 2
    assert cards[0] == StatCardSpec(id="ag-main-gemini", label="AG Main · Gemini")
    assert cards[1] == StatCardSpec(id="ag-main-claude-gpt", label="AG Main · Claude & GPT")

    ds = CollectorConfig(
        id="ds-main",
        label="DeepSeek",
        provider="deepseek",
        machine="host-a",
        key_env="DS_KEY",
    )
    assert ds.stat_cards() == (StatCardSpec(id="ds-main", label="DeepSeek"),)


def test_ui_stats_contribute_emits_cards_for_providers() -> None:
    cfg = UsageConfig(
        collectors=(
            CollectorConfig(
                id="ag",
                label="Antigravity",
                provider="antigravity",
                machine="box",
            ),
            CollectorConfig(
                id="claude",
                label="Claude",
                provider="claude",
                machine="box",
            ),
            CollectorConfig(
                id="disabled-ag",
                label="Disabled AG",
                provider="antigravity",
                machine="box",
                enabled=False,
            ),
        )
    )
    cards = ui_stats.contribute(cfg)
    assert len(cards) == 3
    assert [c.id for c in cards] == ["ag-gemini", "ag-claude-gpt", "claude"]
    assert [c.label for c in cards] == [
        "Antigravity · Gemini",
        "Antigravity · Claude & GPT",
        "Claude",
    ]


def test_antigravity_overlong_id_fails_fast() -> None:
    valid_id = "a" * 53
    ag_valid = CollectorConfig(
        id=valid_id,
        label="AG",
        provider="antigravity",
        machine="box",
    )
    assert len(ag_valid.stat_cards()[1].id) == 64

    overlong_id = "a" * 54
    with pytest.raises(ValueError, match=r"StatCardSpec\.id"):
        CollectorConfig(
            id=overlong_id,
            label="AG",
            provider="antigravity",
            machine="box",
        )


def test_antigravity_overlong_label_fails_fast() -> None:
    valid_label = "x" * 65
    ag_valid = CollectorConfig(
        id="ag-valid",
        label=valid_label,
        provider="antigravity",
        machine="box",
    )
    assert len(ag_valid.stat_cards()[1].label) == 80

    overlong_label = "x" * 66
    with pytest.raises(ValueError, match=r"StatCardSpec\.label"):
        CollectorConfig(
            id="ag-valid",
            label=overlong_label,
            provider="antigravity",
            machine="box",
        )


def test_duplicate_stat_card_id_cross_collector_collision() -> None:
    with pytest.raises(ValueError, match="Duplicate stat card id: 'ag-gemini'"):
        UsageConfig(
            collectors=(
                CollectorConfig(
                    id="ag",
                    label="Antigravity",
                    provider="antigravity",
                    machine="box",
                ),
                CollectorConfig(
                    id="ag-gemini",
                    label="Claude Collision",
                    provider="claude",
                    machine="box",
                ),
            )
        )
