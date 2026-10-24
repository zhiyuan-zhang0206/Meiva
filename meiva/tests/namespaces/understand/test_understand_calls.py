# meiva:note
# The pure side of meiva.understand: which model call a target becomes, how a file's suffix
# classifies it, which configuration is refused before a model is built, and what the saved copy
# of an answer looks like. The tests pass values and assert the returned plan; no model, file or
# clock is involved.

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.lm.effort import ReasoningEffort
from meiva.understand.calls import (
    INLINE_MAX_BYTES,
    MediaSettings,
    check_inline_size,
    media_mime,
    plan_media_call,
    plan_text_call,
)
from meiva.understand.errors import UnderstandError
from meiva.understand.outputs import output_file, result_source, slugify, stale_outputs


@pytest.fixture
def media_model(config_authority: ConfigAuthority) -> str:
    return config_authority.service_field_value("understand_media_model")


def _settings(
    *, resolution: str = "high", thinking_level: str | None = "medium", base_url: str | None = None
) -> MediaSettings:
    return MediaSettings(resolution, thinking_level, base_url)


def test_a_text_call_names_the_model_and_carries_the_effort() -> None:
    call = plan_text_call("deepseek-flash", 30.0, "low")

    assert call.model == "deepseek-flash"
    assert call.desc == "deepseek-flash, text"
    assert call.usage_source == "understand"
    assert call.build.reasoning_effort == "low"
    assert call.build.timeout == 30.0
    assert (call.build.media_resolution, call.build.media_thinking_level) == (None, None)
    assert call.build.base_url is None


def test_an_effort_enum_member_is_the_same_string_as_its_value() -> None:
    effort = plan_text_call("m", 1.0, ReasoningEffort.XHIGH).build.reasoning_effort

    assert effort == "xhigh"
    assert isinstance(effort, str)


@pytest.mark.parametrize(
    ("effort", "level"),
    [("none", "low"), ("low", "low"), ("medium", "medium"), ("high", "high")],
)
def test_media_effort_preserves_the_supported_gemini_levels(
    model_catalog: ModelCatalog, media_model: str, effort: str, level: str
) -> None:
    call = plan_media_call(
        model_catalog,
        media_model,
        mime="image/png",
        timeout=5.0,
        effort=effort,
        settings=_settings(),
    )

    assert call.build.media_thinking_level == level


def test_media_effort_rejects_an_unsupported_grade(
    model_catalog: ModelCatalog, media_model: str
) -> None:
    with pytest.raises(ValueError, match="unsupported reasoning effort"):
        plan_media_call(
            model_catalog,
            media_model,
            mime="image/png",
            timeout=5.0,
            effort="xhigh",
            settings=_settings(),
        )


@pytest.mark.parametrize("configured", ["medium", "high"])
def test_the_default_max_effort_keeps_the_configured_thinking_level(
    model_catalog: ModelCatalog, media_model: str, configured: str
) -> None:
    call = plan_media_call(
        model_catalog,
        media_model,
        mime="image/png",
        timeout=5.0,
        effort=ReasoningEffort.MAX,
        settings=_settings(thinking_level=configured),
    )

    assert call.build.media_thinking_level == configured


def test_a_media_call_carries_the_configured_knobs(
    model_catalog: ModelCatalog, media_model: str
) -> None:
    call = plan_media_call(
        model_catalog,
        media_model,
        mime="image/png,video/mp4",
        timeout=7.0,
        effort=ReasoningEffort.MAX,
        settings=_settings(resolution="low", base_url="http://localhost:8080/v1beta"),
    )

    assert call.model == media_model
    assert call.desc == f"{media_model}, mime=image/png,video/mp4"
    assert call.usage_source == "understand.media"
    assert call.build.media_resolution == "low"
    assert call.build.base_url == "http://localhost:8080/v1beta"
    assert call.build.timeout == 7.0


def test_an_invalid_media_resolution_is_refused_by_the_settings_name(
    model_catalog: ModelCatalog, media_model: str
) -> None:
    with pytest.raises(UnderstandError, match="MEIVA_UNDERSTAND_MEDIA_RESOLUTION"):
        plan_media_call(
            model_catalog,
            media_model,
            mime="image/png",
            timeout=5.0,
            effort=ReasoningEffort.MAX,
            settings=_settings(resolution="ultra"),
        )


def test_a_non_gemini_media_model_fails_fast(model_catalog: ModelCatalog) -> None:
    with pytest.raises(UnderstandError, match="Gemini"):
        plan_media_call(
            model_catalog,
            "gpt-5.6-sol",
            mime="image/png",
            timeout=5.0,
            effort=ReasoningEffort.MAX,
            settings=_settings(),
        )


@pytest.mark.parametrize(
    ("suffix", "mime"),
    [
        (".png", "image/png"),
        (".PNG", "image/png"),
        (".mp4", "video/mp4"),
        (".pdf", "application/pdf"),
    ],
)
def test_a_media_suffix_classifies_to_its_mime_type(suffix: str, mime: str) -> None:
    assert media_mime(suffix) == mime


@pytest.mark.parametrize("suffix", [".md", ".txt", ".py", ".json", ""])
def test_any_other_suffix_is_read_as_text(suffix: str) -> None:
    assert media_mime(suffix) is None


def test_media_over_the_inline_cap_is_refused() -> None:
    check_inline_size(INLINE_MAX_BYTES)
    with pytest.raises(UnderstandError, match="exceeds"):
        check_inline_size(INLINE_MAX_BYTES + 1)


_NOW = datetime(2026, 10, 24, 5, 6, 7, 123456, tzinfo=UTC)


def test_the_saved_copy_is_named_by_time_and_prompt_and_carries_its_origin() -> None:
    saved = output_file("Summarize, please!", "the answer", "text", _NOW)

    assert saved.name == "understand_20261024_050607_123456_summarize-please.txt"
    assert saved.content == (
        "# understand result\n"
        "# prompt: Summarize, please!\n"
        "# source: text\n"
        "# at: 2026-10-24T05:06:07.123456+00:00\n\n"
        "the answer"
    )


def test_a_result_source_lists_the_paths_or_says_text() -> None:
    assert result_source(None) == "text"
    assert result_source(["a.png", "b.png"]) == "paths=['a.png', 'b.png']"


def test_a_slug_is_lowercase_dashed_capped_and_never_empty() -> None:
    assert slugify("  What IS this?  ") == "what-is-this"
    assert slugify("x" * 100) == "x" * 40
    assert slugify("!!!") == "result"


def test_the_ring_keeps_the_newest_names() -> None:
    names = [f"understand_{i:02d}.txt" for i in range(5)]

    assert stale_outputs(names, 3) == ["understand_00.txt", "understand_01.txt"]
    assert stale_outputs(list(reversed(names)), 3) == ["understand_00.txt", "understand_01.txt"]
    assert stale_outputs(names[:2], 3) == []
