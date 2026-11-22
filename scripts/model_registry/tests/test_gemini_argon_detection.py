from __future__ import annotations

from dataclasses import dataclass

from scripts.model_registry.check_model_updates import SOURCES, compare_models


@dataclass(frozen=True)
class _Spec:
    provider: str


def test_gemini_4_argon_is_classified_not_dropped_as_other() -> None:
    source = SOURCES["gemini"]
    registry = {"gemini-4-argon": _Spec("gemini")}

    comparison = compare_models(
        source,
        ["gemini-4-argon", "gemini-4.1-argon", "gemini-3.9-argon", "gemini-pro-latest"],
        registry,
    )

    assert comparison.candidates == ["gemini-4.1-argon"]
    assert comparison.suppressed == ["gemini-3.9-argon"]
    assert comparison.series_models["gemini-4.1-argon"] == ["gemini-4-argon"]
    assert comparison.other_ids == ["gemini-pro-latest"]


def test_gemini_argon_preview_is_its_own_series() -> None:
    source = SOURCES["gemini"]
    registry = {"gemini-4-argon": _Spec("gemini")}

    comparison = compare_models(source, ["gemini-4.1-argon-preview"], registry)

    assert comparison.candidates == ["gemini-4.1-argon-preview"]
    assert comparison.suppressed == []
    assert comparison.series_models["gemini-4.1-argon-preview"] == []


def test_gemini_4_carbon_and_hydrogen_are_classified_as_distinct_series() -> None:
    source = SOURCES["gemini"]
    registry = {
        "gemini-4-argon": _Spec("gemini"),
        "gemini-4-carbon": _Spec("gemini"),
        "gemini-4-hydrogen": _Spec("gemini"),
        "gemini-4-neon": _Spec("gemini"),
    }

    comparison = compare_models(
        source,
        [
            "gemini-4-carbon",
            "gemini-4.1-carbon",
            "gemini-3.9-carbon",
            "gemini-4-hydrogen",
            "gemini-4.1-hydrogen",
            "gemini-3.9-hydrogen",
            "gemini-4-neon",
            "gemini-4.1-neon",
            "gemini-3.9-neon",
        ],
        registry,
    )

    assert comparison.candidates == ["gemini-4.1-carbon", "gemini-4.1-hydrogen", "gemini-4.1-neon"]
    assert comparison.suppressed == ["gemini-3.9-carbon", "gemini-3.9-hydrogen", "gemini-3.9-neon"]
    assert comparison.series_models["gemini-4.1-carbon"] == ["gemini-4-carbon"]
    assert comparison.series_models["gemini-4.1-hydrogen"] == ["gemini-4-hydrogen"]
    assert comparison.series_models["gemini-4.1-neon"] == ["gemini-4-neon"]
    assert comparison.other_ids == []
