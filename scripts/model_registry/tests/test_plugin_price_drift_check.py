# meiva:note
# The gate compares a provider file with the archive it is generated from, so each case
# builds both files in a temporary repository and asserts on the report the hook prints:
# the counts prove the comparison covered something, and the labels name the period a hand
# edit broke.
# meiva:link base/lm/pricing/docs/pricing.meiva.okf.md
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.model_registry import check_plugin_price_drift as cli
from scripts.model_registry.plugin_price_sync import LatticeDrift, lattice_drift

_URL = "https://example.com/pricing"

_LATTICE = """            periods=(
                PricePeriod(
                    effective_from=None,
                    effective_until=None,
                    tiers=(
                        PriceTier(
                            input_tokens_min=0,
                            input_tokens_max=200000,
                            cache_miss="2.0",
                            cache_hit="0.2",
                            output="12.0",
                        ),
                        PriceTier(
                            input_tokens_min=200001,
                            input_tokens_max=None,
                            cache_miss="4.0",
                            cache_hit="0.4",
                            output="18.0",
                        ),
                    ),
                ),
            ),
"""

_PLUGIN = """from base.lm.provider_api import (
    PricePeriod,
    PriceRates,
    PriceTier,
    ProviderContribution,
)

_MODEL = "fixture-chat"


def build(ctx):
    raise NotImplementedError


PROVIDER = ProviderContribution(
    binding=None,
    models={{_MODEL: None}},
    pricing={{
        _MODEL: PriceRates(
            cache_miss={flat_miss},
            cache_hit={flat_hit},
            output={flat_out},
            source_url="{url}",
            source_checked_at="{checked_at}",
            vendor="{vendor}",
{periods}        ),
    }},
)
"""


def _tier(minimum: int, maximum: int | None, rates: tuple[str, str, str]) -> dict[str, Any]:
    return {
        "input_tokens_min": minimum,
        "input_tokens_max": maximum,
        "rates": {"input": rates[0], "cache_read": rates[1], "output": rates[2]},
        "utc_daily_overrides": [],
    }


def _entry(
    tiers: list[dict[str, Any]] | None = None,
    *,
    periods: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "vendor": "typesafe",
        "source_url": _URL,
        "source_checked_at": "2026-10-11",
        "periods": periods
        or [{"effective_from": None, "effective_until": None, "tiers": tiers or []}],
    }


def _source(*, periods: str = "", flat: tuple[str, str, str] = ("1.0", "0.1", "3.0")) -> str:
    return _PLUGIN.format(
        flat_miss=flat[0],
        flat_hit=flat[1],
        flat_out=flat[2],
        url=_URL,
        checked_at="2026-10-11",
        vendor="typesafe",
        periods=periods,
    )


def _repo(tmp_path: Path, *, source: str, models: dict[str, Any]) -> tuple[Path, Path]:
    root = tmp_path / "repo"
    provider = root / "meiva_builtins/plugins/lm_fixture/provider.py"
    provider.parent.mkdir(parents=True)
    provider.write_text(source, encoding="utf-8")
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


def test_a_flat_declaration_matching_the_archive_reports_no_drift(tmp_path: Path) -> None:
    root, archive = _repo(
        tmp_path,
        source=_source(),
        models={"fixture-chat": _entry([_tier(0, None, ("1.0", "0.1", "3.0"))])},
    )

    assert lattice_drift(archive, root) == LatticeDrift(1, 1, ())


def test_a_lattice_declaration_ignores_its_flat_summary(tmp_path: Path) -> None:
    root, archive = _repo(
        tmp_path,
        source=_source(periods=_LATTICE),
        models={
            "fixture-chat": _entry(
                [
                    _tier(0, 200000, ("2.0", "0.2", "12.0")),
                    _tier(200001, None, ("4.0", "0.4", "18.0")),
                ]
            )
        },
    )

    assert lattice_drift(archive, root) == LatticeDrift(1, 1, ())


def test_a_tier_rate_the_archive_does_not_state_names_its_period(tmp_path: Path) -> None:
    drifted = _LATTICE.replace('output="18.0"', 'output="19.0"')
    root, archive = _repo(
        tmp_path,
        source=_source(periods=drifted),
        models={
            "fixture-chat": _entry(
                [
                    _tier(0, 200000, ("2.0", "0.2", "12.0")),
                    _tier(200001, None, ("4.0", "0.4", "18.0")),
                ]
            )
        },
    )

    assert lattice_drift(archive, root).drifted == ("fixture-chat: period 0 [None, None)",)


def test_a_period_the_declaration_omits_is_drift(tmp_path: Path) -> None:
    declaration = _LATTICE.replace(
        "effective_until=None,\n                    tiers",
        'effective_until="2027-01-01T00:00:00Z",\n                    tiers',
        1,
    )
    root, archive = _repo(
        tmp_path,
        source=_source(periods=declaration),
        models={
            "fixture-chat": _entry(
                periods=[
                    {
                        "effective_from": None,
                        "effective_until": "2027-01-01T00:00:00Z",
                        "tiers": [
                            _tier(0, 200000, ("2.0", "0.2", "12.0")),
                            _tier(200001, None, ("4.0", "0.4", "18.0")),
                        ],
                    },
                    {
                        "effective_from": "2027-01-01T00:00:00Z",
                        "effective_until": None,
                        "tiers": [_tier(0, None, ("2.0", "0.2", "6.0"))],
                    },
                ]
            )
        },
    )

    assert lattice_drift(archive, root).drifted == (
        "fixture-chat: period 1 ['2027-01-01T00:00:00Z', None)",
    )


def test_a_flat_declaration_is_compared_by_its_rates(tmp_path: Path) -> None:
    root, archive = _repo(
        tmp_path,
        source=_source(flat=("1.5", "0.1", "3.0")),
        models={"fixture-chat": _entry([_tier(0, None, ("1.0", "0.1", "3.0"))])},
    )

    assert lattice_drift(archive, root).drifted == ("fixture-chat: flat rate",)


def test_a_model_without_an_archive_entry_is_drift(tmp_path: Path) -> None:
    root, archive = _repo(tmp_path, source=_source(), models={})

    assert lattice_drift(archive, root).drifted == (
        "meiva_builtins/plugins/lm_fixture/provider.py: fixture-chat has no archive entry",
    )


def test_a_tree_without_a_provider_file_fails(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    archive = root / "base/lm/pricing/pricing_catalog_archive.json"
    archive.parent.mkdir(parents=True)
    archive.write_text(json.dumps({"models": {}}), encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"no built-in provider\.py files"):
        lattice_drift(archive, root)


def test_the_cli_prints_the_counts_it_compared(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, archive = _repo(
        tmp_path,
        source=_source(),
        models={"fixture-chat": _entry([_tier(0, None, ("1.0", "0.1", "3.0"))])},
    )

    assert cli.main(["--repo-root", str(root), "--catalog", str(archive)]) == 0
    assert "1 provider file(s), 1 priced model(s), 0 drifted" in capsys.readouterr().out


def test_the_cli_fails_on_a_drifted_declaration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, archive = _repo(
        tmp_path,
        source=_source(flat=("1.5", "0.1", "3.0")),
        models={"fixture-chat": _entry([_tier(0, None, ("1.0", "0.1", "3.0"))])},
    )

    assert cli.main(["--repo-root", str(root), "--catalog", str(archive)]) == 1
    assert "fixture-chat: flat rate" in capsys.readouterr().err


_WITHOUT_PRICES = """from base.lm.provider_api import ProviderContribution

PROVIDER = ProviderContribution(binding=None, models={"fixture-chat": None}, pricing={})
"""


def test_the_cli_fails_when_it_compared_no_priced_model(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root, archive = _repo(tmp_path, source=_WITHOUT_PRICES, models={})

    assert cli.main(["--repo-root", str(root), "--catalog", str(archive)]) == 1
    assert "proves nothing" in capsys.readouterr().err
