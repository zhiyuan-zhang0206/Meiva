# meiva:note
# The pure side of meiva.understand: which model a set of content types is routed to, which model
# call a target becomes, how a file's suffix classifies it, which configuration is refused before a model is built, and what the saved copy
# of an answer looks like. The tests pass values and assert the returned plan; no model, file or
# clock is involved.

from __future__ import annotations

import base64
import dataclasses
from datetime import UTC, datetime

import pytest

from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.lm.effort import ReasoningEffort
from base.lm.registry import ModelSpec
from base.packages.tests.model_catalog import AddModels
from meiva.understand.calls import MediaSettings, media_mime, plan_call, plan_text_call
from meiva.understand.content import MediaPart, TextPart, content_for, content_types
from meiva.understand.errors import UnderstandError
from meiva.understand.outputs import output_file, result_source, slugify, stale_outputs
from meiva.understand.routing import ROUTE_ORDER, UnderstandModels, model_setting, route

_PNG = b"\x89PNG\r\n\x1a\n"
_DEFAULTS = UnderstandModels(
    text="deepseek-flash",
    image="deepseek-flash",
    pdf="gemini-3.8-flash",
    audio="gemini-3.8-flash",
    video="gemini-3.8-flash",
)


@pytest.fixture
def media_model(config_authority: ConfigAuthority) -> str:
    return config_authority.service_field_value("understand_video_model")


def _settings(
    *, resolution: str = "high", thinking_level: str | None = "medium", base_url: str | None = None
) -> MediaSettings:
    return MediaSettings(resolution, thinking_level, base_url)


def _call(
    catalog: ModelCatalog,
    model: str,
    *,
    types: frozenset[str] = frozenset({"image"}),
    effort: str | ReasoningEffort = ReasoningEffort.MAX,
    settings: MediaSettings | None = None,
    timeout: float = 5.0,
):
    return plan_call(
        catalog,
        model,
        types,
        timeout=timeout,
        effort=effort,
        settings=settings or _settings(),
    )


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
def test_gemini_media_effort_preserves_the_supported_levels(
    model_catalog: ModelCatalog, media_model: str, effort: str, level: str
) -> None:
    call = _call(model_catalog, media_model, effort=effort)

    assert call.build.media_thinking_level == level


def test_gemini_media_effort_rejects_an_unsupported_grade(
    model_catalog: ModelCatalog, media_model: str
) -> None:
    with pytest.raises(ValueError, match="unsupported reasoning effort"):
        _call(model_catalog, media_model, effort="xhigh")


@pytest.mark.parametrize("configured", ["medium", "high"])
def test_the_default_max_effort_keeps_the_configured_thinking_level(
    model_catalog: ModelCatalog, media_model: str, configured: str
) -> None:
    call = _call(model_catalog, media_model, settings=_settings(thinking_level=configured))

    assert call.build.media_thinking_level == configured


def test_a_gemini_media_call_carries_the_configured_knobs(
    model_catalog: ModelCatalog, media_model: str
) -> None:
    call = _call(
        model_catalog,
        media_model,
        types=frozenset({"image", "video"}),
        timeout=7.0,
        settings=_settings(resolution="low", base_url="http://localhost:8080/v1beta"),
    )

    assert call.model == media_model
    assert call.desc == f"{media_model}, video+image"
    assert call.usage_source == "understand.video"
    assert call.build.media_resolution == "low"
    assert call.build.base_url == "http://localhost:8080/v1beta"
    assert call.build.timeout == 7.0


def test_a_non_gemini_media_call_carries_the_effort_and_none_of_the_gemini_knobs(
    model_catalog: ModelCatalog,
) -> None:
    call = _call(
        model_catalog,
        "deepseek-flash",
        effort="high",
        settings=_settings(resolution="low", thinking_level="low", base_url="http://relay"),
    )

    assert call.model == "deepseek-flash"
    assert call.usage_source == "understand.image"
    assert call.build.reasoning_effort == "high"
    assert (call.build.media_resolution, call.build.media_thinking_level) == (None, None)
    assert call.build.base_url is None


def test_an_invalid_media_resolution_is_refused_by_the_settings_name_on_a_gemini_model(
    model_catalog: ModelCatalog, media_model: str
) -> None:
    with pytest.raises(UnderstandError, match="MEIVA_UNDERSTAND_MEDIA_RESOLUTION"):
        _call(model_catalog, media_model, settings=_settings(resolution="ultra"))


def test_an_invalid_media_resolution_does_not_matter_to_a_non_gemini_model(
    model_catalog: ModelCatalog,
) -> None:
    call = _call(model_catalog, "deepseek-flash", settings=_settings(resolution="ultra"))

    assert call.build.media_resolution is None


def test_the_usage_source_names_the_richest_type_of_the_content(
    model_catalog: ModelCatalog,
) -> None:
    pdf_and_image = frozenset({"pdf", "image", "text"})

    call = _call(model_catalog, "gemini-3.8-flash", types=pdf_and_image)

    assert call.usage_source == "understand.pdf"
    assert _call(model_catalog, "deepseek-flash", types=frozenset({"text"})).usage_source == (
        "understand"
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


def test_the_content_types_of_parts_are_text_or_the_media_type() -> None:
    parts = [
        TextPart("notes"),
        MediaPart("a.png", "image/png", _PNG),
        MediaPart("b.pdf", "application/pdf", b"%PDF"),
        MediaPart("c.mp3", "audio/mpeg", b"\x00"),
    ]

    assert content_types(parts) == {"text", "image", "pdf", "audio"}


def test_the_route_order_is_video_audio_pdf_image_text() -> None:
    assert ROUTE_ORDER == ("video", "audio", "pdf", "image", "text")


def test_a_model_setting_is_named_by_its_environment_variable() -> None:
    assert model_setting("pdf") == "MEIVA_UNDERSTAND_PDF_MODEL"


@pytest.mark.parametrize(
    ("types", "model"),
    [
        ({"text"}, "deepseek-flash"),
        ({"image"}, "deepseek-flash"),
        ({"image", "text"}, "deepseek-flash"),
        ({"pdf"}, "gemini-3.8-flash"),
        ({"audio"}, "gemini-3.8-flash"),
        ({"video", "text"}, "gemini-3.8-flash"),
        ({"pdf", "image"}, "gemini-3.8-flash"),
        ({"audio", "image", "pdf", "video", "text"}, "gemini-3.8-flash"),
    ],
)
def test_the_default_models_route_each_set_of_types(
    model_catalog: ModelCatalog, types: set[str], model: str
) -> None:
    assert route(model_catalog, _DEFAULTS, frozenset(types)) == model


def test_the_first_candidate_in_route_order_that_accepts_every_type_answers(
    model_catalog: ModelCatalog,
) -> None:
    models = dataclasses.replace(_DEFAULTS, pdf="claude-sonnet-5", video="gemini-3.1-pro-preview")

    assert route(model_catalog, models, frozenset({"pdf", "image"})) == "claude-sonnet-5"
    assert route(model_catalog, models, frozenset({"video", "pdf"})) == "gemini-3.1-pro-preview"


def test_a_later_candidate_answers_when_an_earlier_one_cannot_accept_every_type(
    model_catalog: ModelCatalog, add_models: AddModels
) -> None:
    catalog = add_models(
        model_catalog,
        {"pdf-only": ModelSpec(provider="anthropic", media_types=frozenset({"pdf"}))},
    )
    models = dataclasses.replace(_DEFAULTS, pdf="pdf-only", image="claude-sonnet-5")

    assert route(catalog, models, frozenset({"pdf", "image"})) == "claude-sonnet-5"


def test_no_model_accepting_every_type_names_the_types_and_the_configured_models(
    model_catalog: ModelCatalog, add_models: AddModels
) -> None:
    catalog = add_models(
        model_catalog,
        {"pdf-only": ModelSpec(provider="anthropic", media_types=frozenset({"pdf"}))},
    )
    models = dataclasses.replace(_DEFAULTS, pdf="pdf-only")

    with pytest.raises(UnderstandError) as raised:
        route(catalog, models, frozenset({"pdf", "image"}))

    message = str(raised.value)
    assert "['image', 'pdf']" in message
    assert "MEIVA_UNDERSTAND_PDF_MODEL='pdf-only'" in message
    assert "MEIVA_UNDERSTAND_IMAGE_MODEL='deepseek-flash'" in message


@pytest.mark.parametrize(
    ("content_type", "setting"),
    [
        ("image", "MEIVA_UNDERSTAND_IMAGE_MODEL"),
        ("pdf", "MEIVA_UNDERSTAND_PDF_MODEL"),
        ("audio", "MEIVA_UNDERSTAND_AUDIO_MODEL"),
        ("video", "MEIVA_UNDERSTAND_VIDEO_MODEL"),
    ],
)
def test_a_model_not_declaring_its_own_type_is_a_configuration_error_naming_the_setting(
    model_catalog: ModelCatalog, content_type: str, setting: str
) -> None:
    models = dataclasses.replace(_DEFAULTS, **{content_type: "text-only-model"})

    with pytest.raises(UnderstandError, match=f"{setting}='.*' does not accept {content_type}"):
        route(model_catalog, models, frozenset({content_type}))


def test_a_misconfigured_type_that_is_absent_does_not_matter(model_catalog: ModelCatalog) -> None:
    models = dataclasses.replace(_DEFAULTS, audio="deepseek-flash")

    assert route(model_catalog, models, frozenset({"image"})) == "deepseek-flash"


def test_content_is_the_parts_in_order_with_the_prompt_last(model_catalog: ModelCatalog) -> None:
    parts = [TextPart("notes"), MediaPart("a.png", "image/png", _PNG, dimensions=(2, 2))]

    content = content_for(model_catalog, "deepseek-flash", parts, "what is this")

    encoded = base64.b64encode(_PNG).decode("ascii")
    assert content == [
        {"type": "text", "text": "notes"},
        {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{encoded}"}},
        {"type": "text", "text": "what is this"},
    ]


def test_a_gemini_pdf_and_a_claude_pdf_are_built_for_their_provider(
    model_catalog: ModelCatalog,
) -> None:
    parts = [MediaPart("a.pdf", "application/pdf", b"%PDF")]

    [gemini, _] = content_for(model_catalog, "gemini-3.8-flash", parts, "p")
    [claude, _] = content_for(model_catalog, "claude-sonnet-5", parts, "p")

    assert gemini == {"type": "media", "mime_type": "application/pdf", "data": b"%PDF"}
    assert claude["type"] == "document"


def test_a_media_file_over_the_shared_ceiling_is_refused_naming_the_path(
    model_catalog: ModelCatalog,
) -> None:
    big = MediaPart("shots/huge.png", "image/png", b"\x00" * (20 * 1024 * 1024 + 1), (2, 2))

    with pytest.raises(UnderstandError, match=r"'shots/huge\.png' cannot be sent to .*20 MiB"):
        content_for(model_catalog, "deepseek-flash", [big], "p")


def test_an_image_over_the_model_dimension_limit_is_refused_naming_path_dimensions_model_and_limit(
    model_catalog: ModelCatalog,
) -> None:
    wide = MediaPart("shots/wide.png", "image/png", _PNG, dimensions=(9000, 120))

    with pytest.raises(
        UnderstandError,
        match=(
            r"'shots/wide\.png' cannot be sent to deepseek-flash: "
            r"image \(9000x120 px\) exceeds 8192 px dimension limit"
        ),
    ):
        content_for(model_catalog, "deepseek-flash", [wide], "p")


def test_the_15th_image_uses_the_multi_image_dimension_tier_of_the_model(
    model_catalog: ModelCatalog,
) -> None:
    parts = [
        *(
            MediaPart(f"shots/{index}.png", "image/png", _PNG, dimensions=(8000, 2))
            for index in range(1, 15)
        ),
        MediaPart("shots/15.png", "image/png", _PNG, dimensions=(4097, 2)),
    ]

    with pytest.raises(
        UnderstandError,
        match=(
            r"'shots/15\.png' cannot be sent to deepseek-flash: "
            r"image \(4097x2 px\) exceeds 4096 px dimension limit"
        ),
    ):
        content_for(model_catalog, "deepseek-flash", parts, "p")


def test_an_image_whose_dimensions_cannot_be_read_is_refused_naming_path_and_model(
    model_catalog: ModelCatalog,
) -> None:
    broken = MediaPart("shots/broken.png", "image/png", _PNG, dimensions=None)

    with pytest.raises(
        UnderstandError,
        match=r"'shots/broken\.png' cannot be sent to deepseek-flash: cannot read image dimensions",
    ):
        content_for(model_catalog, "deepseek-flash", [broken], "p")


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
