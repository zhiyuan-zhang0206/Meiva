# meiva:note
# Which declarations one ``provider.py`` contributes, and how a broken one surfaces. A
# decision provider's prices are billed from the same ``PriceBook`` as chat prices, so a
# declaration the parser skips would keep stale rates silently; a declaration it cannot read
# must stop the whole pass and write nothing.
from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from scripts.model_registry import plugin_price_sync as sync
from scripts.model_registry import update_model_pricing as pricing_updater
from scripts.model_registry.plugin_price_source import provider_manifest

_URL = "https://example.com/decisions"

_DECISION_PLUGIN = """from base.lm.provider_api import (
    DecisionModelSpec,
    DecisionProviderContribution,
    PriceRates,
)

_MODEL = "jev-9.9.9"


def build(ctx):
    raise NotImplementedError


PROVIDER = DecisionProviderContribution(
    prefix="jev-",
    display_name="TypeSafe",
    key_env="TYPESAFE_API_KEY",
    build=build,
    models={_MODEL: DecisionModelSpec(context_window=32_000)},
    pricing={
        _MODEL: PriceRates(
            cache_miss=0.042,
            cache_hit=0.042,
            output=0.0,
            source_url="https://example.com/decisions",
            source_checked_at="2026-10-11",
            vendor="typesafe",
        ),
    },
)
"""

_CHAT_AND_DECISION_PLUGIN = """from base.lm.provider_api import (
    DecisionModelSpec,
    DecisionProviderContribution,
    PriceRates,
    ProviderContribution,
)


def build(ctx):
    raise NotImplementedError


PROVIDER = ProviderContribution(
    binding=None,
    models={"chat-model": None},
    pricing={
        "chat-model": PriceRates(
            cache_miss=1.0,
            cache_hit=0.1,
            output=3.0,
            source_url="https://example.com/decisions",
            source_checked_at="2026-10-11",
            vendor="typesafe",
        ),
    },
)

DECISION_PROVIDER = DecisionProviderContribution(
    prefix="jev-",
    display_name="TypeSafe",
    key_env="TYPESAFE_API_KEY",
    build=build,
    models={"decision-model": DecisionModelSpec(context_window=32_000)},
    pricing={
        "decision-model": PriceRates(
            cache_miss=0.042,
            cache_hit=0.042,
            output=0.0,
            source_url="https://example.com/decisions",
            source_checked_at="2026-10-11",
            vendor="typesafe",
        ),
    },
)
"""


def _entry(rates: tuple[str, str, str], *, checked_at: str = "2026-10-11") -> dict[str, Any]:
    return {
        "vendor": "typesafe",
        "source_url": _URL,
        "source_checked_at": checked_at,
        "periods": [
            {
                "effective_from": None,
                "effective_until": None,
                "tiers": [
                    {
                        "input_tokens_min": 0,
                        "input_tokens_max": None,
                        "rates": {
                            "input": rates[0],
                            "cache_read": rates[1],
                            "output": rates[2],
                        },
                        "utc_daily_overrides": [],
                    }
                ],
            }
        ],
    }


def _repo(
    tmp_path: Path, sources: dict[str, str], models: dict[str, dict[str, Any]]
) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    for plugin, source in sources.items():
        path = root / f"meiva_builtins/plugins/{plugin}/provider.py"
        path.parent.mkdir(parents=True)
        path.write_text(source, encoding="utf-8")
    archive = root / "base/lm/pricing/pricing_catalog_archive.json"
    archive.parent.mkdir(parents=True)
    archive.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "catalog_version": "fixture",
                "currency": "USD",
                "unit_tokens": 1_000_000,
                "models": models,
            }
        ),
        encoding="utf-8",
    )
    return root, archive


def test_a_decision_provider_contribution_is_synchronized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, archive = _repo(
        tmp_path,
        {"lm_fixture": _DECISION_PLUGIN},
        {"jev-9.9.9": _entry(("0.05", "0.05", "0"), checked_at="2026-10-20")},
    )
    provider = root / "meiva_builtins/plugins/lm_fixture/provider.py"

    result = pricing_updater.sync_plugin_rates(archive, root)

    assert result.drifted_models == ("jev-9.9.9",)
    rewritten = provider.read_text(encoding="utf-8")
    assert provider_manifest(rewritten, provider).prices["jev-9.9.9"].rates == sync.FlatRates(
        Decimal("0.05"), Decimal("0.05"), Decimal("0")
    )
    assert "periods=(" not in rewritten
    assert (
        "meiva_builtins/plugins/lm_fixture/provider.py: PROVIDER — 1 priced model(s), 1 drifted\n"
        in capsys.readouterr().out
    )


def test_every_declaration_in_one_provider_file_is_synchronized(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, archive = _repo(
        tmp_path,
        {"lm_fixture": _CHAT_AND_DECISION_PLUGIN},
        {
            "chat-model": _entry(("2.0", "0.2", "6.0"), checked_at="2026-10-20"),
            "decision-model": _entry(("0.07", "0.07", "0"), checked_at="2026-10-20"),
        },
    )
    provider = root / "meiva_builtins/plugins/lm_fixture/provider.py"

    result = pricing_updater.sync_plugin_rates(archive, root)

    assert result.drifted_models == ("chat-model", "decision-model")
    rewritten = provider.read_text(encoding="utf-8")
    prices = provider_manifest(rewritten, provider).prices
    assert prices["chat-model"].rates == sync.FlatRates(
        Decimal("2.0"), Decimal("0.2"), Decimal("6.0")
    )
    assert prices["decision-model"].rates == sync.FlatRates(
        Decimal("0.07"), Decimal("0.07"), Decimal("0")
    )
    assert "PROVIDER, DECISION_PROVIDER — 2 priced model(s), 2 drifted" in capsys.readouterr().out


def test_a_declaration_type_must_come_from_the_provider_contract(tmp_path: Path) -> None:
    source = _DECISION_PLUGIN.replace(
        "from base.lm.provider_api import", "from base.lm.other import"
    )

    with pytest.raises(RuntimeError, match="expected a top-level"):
        provider_manifest(source, tmp_path / "provider.py")


def test_a_malformed_declaration_fails_loudly_and_writes_nothing(tmp_path: Path) -> None:
    broken = _DECISION_PLUGIN.replace(
        "models={_MODEL: DecisionModelSpec(context_window=32_000)}",
        "models=dynamic_models",
    )
    root, archive = _repo(
        tmp_path,
        {"lm_broken": broken, "lm_fixture": _DECISION_PLUGIN},
        {
            "jev-9.9.9": _entry(("0.05", "0.05", "0"), checked_at="2026-10-20"),
        },
    )
    healthy = root / "meiva_builtins/plugins/lm_fixture/provider.py"
    before = healthy.read_text(encoding="utf-8")

    with pytest.raises(RuntimeError, match="must have one literal models= mapping"):
        pricing_updater.sync_plugin_rates(archive, root)

    assert healthy.read_text(encoding="utf-8") == before


def test_a_lattice_the_module_does_not_import_is_refused(tmp_path: Path) -> None:
    drifted = _entry(("0.05", "0.05", "0"), checked_at="2026-10-20")
    drifted["periods"][0]["effective_until"] = "2027-01-01T00:00:00Z"
    drifted["periods"].append(
        {
            "effective_from": "2027-01-01T00:00:00Z",
            "effective_until": None,
            "tiers": [
                {
                    "input_tokens_min": 0,
                    "input_tokens_max": None,
                    "rates": {"input": "0.06", "cache_read": "0.06", "output": "0"},
                    "utc_daily_overrides": [],
                }
            ],
        }
    )
    root, archive = _repo(tmp_path, {"lm_fixture": _DECISION_PLUGIN}, {"jev-9.9.9": drifted})

    with pytest.raises(RuntimeError, match="does not import from the provider contract"):
        pricing_updater.sync_plugin_rates(archive, root)
