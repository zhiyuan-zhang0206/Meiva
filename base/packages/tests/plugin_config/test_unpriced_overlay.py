# meiva:note
# An overlay may only name a model the pricing catalog can price (2026-10-30, #5832).
# `gpt-6.1-sol` was called for four hours before a deployment carried its price: usage was
# recorded with cost 0, so the bill undercounted silently. These tests pin the door: a named
# model without a price is refused at spawn/restart, and the repo's own default models must
# stay priced.
# meiva:link base/lm/docs/pricing-model-rates.meiva.okf.md

from dataclasses import dataclass
from pathlib import Path

import pytest

from base.config.domains.lm import LmSettings
from base.lm.pricing.archive import load_archive
from base.packages.plugins.config_registration import validate_config_overlay
from base.packages.plugins.plugin_errors import InvalidConfigOverlay


@dataclass(frozen=True)
class _Withdrawn:
    unavailable_fallback: str


MODELS = {
    "priced-model": object(),
    "unpriced-model": object(),
    "withdrawn-priced": _Withdrawn(unavailable_fallback="priced-model"),
    "withdrawn-unpriced": _Withdrawn(unavailable_fallback="unpriced-model"),
}


def _home(tmp_path: Path) -> Path:
    return tmp_path / "home"


def test_an_unpriced_model_is_refused_with_the_reason_and_the_place(tmp_path: Path) -> None:
    with pytest.raises(InvalidConfigOverlay) as caught:
        validate_config_overlay(
            {"llm_model": "unpriced-model"},
            models=MODELS,
            prices={"priced-model"},
            home=_home(tmp_path),
        )
    message = str(caught.value)
    assert "no price in the pricing catalog" in message
    assert "undercount" in message
    assert "pricing_catalog_archive.json" in message


def test_a_priced_model_passes(tmp_path: Path) -> None:
    validate_config_overlay(
        {"llm_model": "priced-model"},
        models=MODELS,
        prices={"priced-model"},
        home=_home(tmp_path),
    )


def test_a_caller_that_supplies_no_prices_keeps_the_membership_only_contract(
    tmp_path: Path,
) -> None:
    # meiva:note
    # `prices` is opt-in so that callers without a catalog (config-shape checks, child
    # processes) do not grow a second failure mode; the admission paths all pass it.
    validate_config_overlay({"llm_model": "unpriced-model"}, models=MODELS, home=_home(tmp_path))


def test_a_withdrawn_model_is_judged_by_the_model_it_settles_to(tmp_path: Path) -> None:
    # meiva:note
    # A withdrawn id names `unavailable_fallback`, and the runtime settles it in one hop before
    # calling it; judging the withdrawn id itself would refuse an overlay the runtime can price.
    validate_config_overlay(
        {"llm_model": "withdrawn-priced"},
        models=MODELS,
        prices={"priced-model"},
        home=_home(tmp_path),
    )


def test_a_withdrawn_model_settling_to_an_unpriced_model_is_refused(tmp_path: Path) -> None:
    with pytest.raises(InvalidConfigOverlay, match="no price in the pricing catalog"):
        validate_config_overlay(
            {"llm_model": "withdrawn-unpriced"},
            models=MODELS,
            prices={"priced-model"},
            home=_home(tmp_path),
        )


def test_every_default_model_in_the_repo_is_priced() -> None:
    # meiva:note
    # The cluster default, the labeler and the hierarchy model are named by config, not by a
    # spawn request, so a missing price there cannot be refused at an overlay — it would show
    # up only as zero-cost usage. This is the CI-side half of the same door.
    archive = load_archive()
    settings = LmSettings()
    named = {
        "llm_model": settings.llm_model,
        "labeler_model": settings.labeler_model,
        "hierarchy_model": settings.hierarchy_model,
    }
    unpriced = {field: model for field, model in named.items() if model not in archive}
    assert unpriced == {}
