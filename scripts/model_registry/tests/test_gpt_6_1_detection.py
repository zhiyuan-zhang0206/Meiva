from __future__ import annotations

from dataclasses import dataclass

from scripts.model_registry.check_model_updates import SOURCES, compare_models


@dataclass(frozen=True)
class _Spec:
    provider: str


def test_gpt_6_1_sol_is_a_newer_same_series_candidate() -> None:
    source = SOURCES["gpt"]
    registry = {"gpt-6-sol": _Spec("gpt")}

    comparison = compare_models(source, ["gpt-6.1-sol"], registry)

    assert comparison.candidates == ["gpt-6.1-sol"]
    assert comparison.suppressed == []
    assert comparison.other_ids == []
    assert comparison.series_models["gpt-6.1-sol"] == ["gpt-6-sol"]
