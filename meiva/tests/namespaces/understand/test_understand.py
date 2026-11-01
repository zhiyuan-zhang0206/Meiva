# meiva:note
# `meiva.understand` end to end over deps built from the test configuration: files are real
# files under `tmp_path`, and the model of each content type is scripted behind the real provider
# factory. Which call a target becomes (model, effort, media knobs) and which model a mix of types
# is routed to are asserted on values in test_understand_calls.py; these tests cover the shell:
# what reaches the model, which model answers, and how failures surface.

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any, cast

import pytest
from langchain_core.exceptions import ModelRateLimitError
from langchain_core.messages import AIMessage

from meiva.sdk_surface.pure.plans.batch_plan import DEFAULT_BATCH_MAX_CONCURRENT
from meiva.tests.namespaces.understand.provider_support import UnderstandHarness, image_block
from meiva.tests.namespaces.understand.provider_support import fake_image as fake_image
from meiva.tests.namespaces.understand.provider_support import fake_pdf as fake_pdf
from meiva.tests.namespaces.understand.provider_support import fake_video as fake_video
from meiva.tests.namespaces.understand.provider_support import (
    understand_harness as understand_harness,
)
from meiva.understand import UnderstandError, understand


def test_text_mode_sends_the_material_then_the_prompt_to_the_text_model(
    understand_harness: UnderstandHarness,
) -> None:
    [out] = understand_harness.understand([{"prompt": "summarize", "text": "the quick brown fox"}])

    assert out == "fake answer"
    assert understand_harness.pdf.requests == []
    assert understand_harness.content_of(understand_harness.text) == [
        {"type": "text", "text": "the quick brown fox"},
        {"type": "text", "text": "summarize"},
    ]


def test_effort_invalid_value_raises(understand_harness: UnderstandHarness) -> None:
    with pytest.raises(ValueError, match="effort"):
        understand_harness.understand([{"prompt": "x", "text": "y"}], effort="ultra")
    with pytest.raises(ValueError, match="effort"):
        understand_harness.understand([{"prompt": "x", "text": "y"}], effort="minimal")
    with pytest.raises(TypeError, match="effort"):
        understand_harness.understand([{"prompt": "x", "text": "y"}], effort=cast(Any, 3))
    assert understand_harness.text.requests == []


def test_gemini_media_effort_rejects_an_unsupported_grade_before_asking_the_model(
    understand_harness: UnderstandHarness, fake_pdf: Path
) -> None:
    with pytest.raises(ValueError, match="unsupported reasoning effort"):
        understand_harness.understand([{"prompt": "x", "paths": [str(fake_pdf)]}], effort="xhigh")

    assert understand_harness.pdf.requests == []


def test_existing_text_file_goes_to_the_text_model(
    understand_harness: UnderstandHarness, tmp_path: Path
) -> None:
    f = tmp_path / "notes.md"
    f.write_text("# Heading\nbody text", encoding="utf-8")

    understand_harness.understand([{"prompt": "what is the heading", "paths": [str(f)]}])

    assert understand_harness.pdf.requests == []
    assert understand_harness.content_of(understand_harness.text) == [
        {"type": "text", "text": "# Heading\nbody text"},
        {"type": "text", "text": "what is the heading"},
    ]


def test_path_not_found_raises(understand_harness: UnderstandHarness, tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        understand_harness.understand([{"prompt": "x", "paths": [str(tmp_path / "nope.txt")]}])


def test_single_call_keywords_rejected(tmp_path: Path) -> None:
    f = tmp_path / "f.txt"
    f.write_text("x")
    call_understand = cast(Any, understand)
    with pytest.raises(TypeError):
        call_understand(prompt="x", path=str(f))
    with pytest.raises(TypeError):
        call_understand(prompt="x", text="y")


def test_a_relative_path_resolves_against_the_base_directory(
    understand_harness: UnderstandHarness, tmp_path: Path
) -> None:
    base = tmp_path / "workspace"
    base.mkdir()
    (base / "notes.md").write_text("workspace material", encoding="utf-8")
    understand_harness.configure(base_dir=base)

    understand_harness.understand([{"prompt": "p", "paths": ["notes.md"]}])

    assert understand_harness.content_of(understand_harness.text)[0] == {
        "type": "text",
        "text": "workspace material",
    }


def test_a_content_list_answer_is_flattened_to_its_text_blocks(
    understand_harness: UnderstandHarness,
) -> None:
    understand_harness.text.replies = (
        AIMessage(
            content=[
                {"type": "text", "text": "line one"},
                {"type": "thinking", "text": "ignored"},
                {"type": "text", "text": "line two"},
            ]
        ),
    )

    [out] = understand_harness.understand([{"prompt": "prompt", "text": "material"}])

    assert out == "line one\nline two"


def test_an_image_goes_to_the_image_model_as_a_data_uri(
    understand_harness: UnderstandHarness, fake_image: Path
) -> None:
    understand_harness.configure_models(image="claude-sonnet-5")

    [out] = understand_harness.understand([{"prompt": "describe this", "paths": [str(fake_image)]}])

    assert out == "fake answer"
    assert understand_harness.text.requests == []
    assert understand_harness.pdf.requests == []
    assert understand_harness.content_of(understand_harness.image) == [
        image_block(fake_image.read_bytes()),
        {"type": "text", "text": "describe this"},
    ]


def test_by_default_an_image_is_answered_by_the_text_model_not_a_gemini_one(
    understand_harness: UnderstandHarness, fake_image: Path
) -> None:
    understand_harness.understand([{"prompt": "describe this", "paths": [str(fake_image)]}])

    assert understand_harness.deps.models.image == understand_harness.deps.models.text
    assert len(understand_harness.image.requests) == 1
    assert understand_harness.pdf.requests == []


def test_video_and_pdf_carry_their_own_mime_types(
    understand_harness: UnderstandHarness, fake_video: Path, fake_pdf: Path
) -> None:
    understand_harness.understand([{"prompt": "summarize", "paths": [str(fake_video)]}])
    assert understand_harness.content_of(understand_harness.video)[0]["mime_type"] == "video/mp4"

    understand_harness.understand([{"prompt": "summarize", "paths": [str(fake_pdf)]}])
    assert (
        understand_harness.content_of(understand_harness.pdf)[0]["mime_type"] == "application/pdf"
    )


def test_a_pdf_goes_to_a_claude_pdf_model_as_a_document_block(
    understand_harness: UnderstandHarness, fake_pdf: Path
) -> None:
    understand_harness.configure_models(pdf="claude-sonnet-5")

    understand_harness.understand([{"prompt": "summarize", "paths": [str(fake_pdf)]}])

    assert understand_harness.content_of(understand_harness.pdf)[0]["type"] == "document"


def test_each_media_type_goes_to_its_own_configured_model(
    understand_harness: UnderstandHarness, tmp_path: Path
) -> None:
    understand_harness.configure_models(
        image="claude-sonnet-5", pdf="gemini-3.1-pro-preview", audio="gemini-3.8-flash"
    )
    (tmp_path / "a.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.7\n")
    (tmp_path / "a.mp3").write_bytes(b"\x00" * 8)

    for name in ("a.png", "a.pdf", "a.mp3"):
        understand_harness.understand([{"prompt": "p", "paths": [name]}])

    assert len(understand_harness.image.requests) == 1
    assert len(understand_harness.pdf.requests) == 1
    assert len(understand_harness.audio.requests) == 1
    assert understand_harness.text.requests == []


def test_the_configured_text_model_answers(understand_harness: UnderstandHarness) -> None:
    understand_harness.configure_models(text="claude-sonnet-5")

    understand_harness.understand([{"prompt": "x", "text": "some text"}])

    assert len(understand_harness.text.requests) == 1


def test_the_configured_video_model_answers(
    understand_harness: UnderstandHarness, fake_video: Path
) -> None:
    understand_harness.configure_models(video="gemini-3.1-pro-preview")

    understand_harness.understand([{"prompt": "x", "paths": [str(fake_video)]}])

    assert len(understand_harness.video.requests) == 1
    assert understand_harness.pdf.requests == []


def test_a_missing_media_key_surfaces_as_an_understand_error(
    understand_harness: UnderstandHarness, fake_pdf: Path
) -> None:
    understand_harness.scripted_models.refuse(
        understand_harness.deps.models.pdf, RuntimeError("GEMINI_API_KEY not set")
    )

    with pytest.raises(UnderstandError, match="GEMINI_API_KEY"):
        understand_harness.understand([{"prompt": "x", "paths": [str(fake_pdf)]}])


def test_a_missing_text_key_surfaces_as_an_understand_error(
    understand_harness: UnderstandHarness,
) -> None:
    understand_harness.scripted_models.refuse(
        understand_harness.deps.models.text, RuntimeError("DEEPSEEK_API_KEY not set")
    )

    with pytest.raises(UnderstandError, match="DEEPSEEK_API_KEY"):
        understand_harness.understand([{"prompt": "x", "text": "some text"}])


def test_oversized_media_is_refused_before_any_model_is_asked(
    understand_harness: UnderstandHarness, tmp_path: Path
) -> None:
    big = tmp_path / "huge.mp4"
    big.write_bytes(b"\x00" * (20 * 1024 * 1024 + 1))

    with pytest.raises(UnderstandError, match=r"huge\.mp4.*file exceeds 20 MiB limit"):
        understand_harness.understand([{"prompt": "x", "paths": [str(big)]}])

    assert understand_harness.video.requests == []


def test_an_undecodable_unknown_file_is_refused_with_the_suffix_hint(
    understand_harness: UnderstandHarness, tmp_path: Path
) -> None:
    weird = tmp_path / "data.bin"
    weird.write_bytes(b"\xff\xfe\x00\x01\x80")

    with pytest.raises(UnderstandError, match="UTF-8"):
        understand_harness.understand([{"prompt": "x", "paths": [str(weird)]}])


def test_an_empty_media_answer_is_refused(
    understand_harness: UnderstandHarness, fake_image: Path
) -> None:
    understand_harness.image.replies = ("",)

    with pytest.raises(UnderstandError, match="empty response"):
        understand_harness.understand([{"prompt": "x", "paths": [str(fake_image)]}])


def test_a_typed_provider_failure_is_wrapped_keeping_its_cause(
    understand_harness: UnderstandHarness,
) -> None:
    understand_harness.configure(
        retry=dataclasses.replace(understand_harness.deps.retry, attempts=0)
    )
    error = ModelRateLimitError("rate limit")
    understand_harness.text.replies = (error,)

    with pytest.raises(UnderstandError, match="rate limit") as raised:
        understand_harness.understand([{"prompt": "x", "text": "some text"}])

    assert raised.value.__cause__ is error
    assert len(understand_harness.text.requests) == 1


@pytest.mark.parametrize(
    "error",
    [
        TypeError("invalid callback input"),
        ValueError("invalid response"),
        RuntimeError("rate limit"),
    ],
)
def test_unknown_model_error_preserves_identity_without_retry(
    understand_harness: UnderstandHarness, error: Exception
) -> None:
    understand_harness.text.replies = (error,)

    with pytest.raises(type(error)) as raised:
        understand_harness.understand([{"prompt": "x", "text": "some text"}])

    assert raised.value is error
    assert len(understand_harness.text.requests) == 1


def test_paths_two_images_make_a_single_media_call(
    understand_harness: UnderstandHarness, fake_image: Path, tmp_path: Path
) -> None:
    img2 = tmp_path / "img2.png"
    img2.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x11" * 32)

    [out] = understand_harness.understand(
        [{"prompt": "compare these", "paths": [str(fake_image), str(img2)]}]
    )

    assert out == "fake answer"
    assert len(understand_harness.image.requests) == 1
    assert understand_harness.content_of(understand_harness.image) == [
        image_block(fake_image.read_bytes()),
        image_block(img2.read_bytes()),
        {"type": "text", "text": "compare these"},
    ]


def test_paths_mixed_media_and_text_go_to_the_media_type_model(
    understand_harness: UnderstandHarness, fake_image: Path, tmp_path: Path
) -> None:
    understand_harness.configure_models(image="claude-sonnet-5")
    notes = tmp_path / "notes.md"
    notes.write_text("design notes", encoding="utf-8")

    understand_harness.understand([{"prompt": "check", "paths": [str(fake_image), str(notes)]}])

    assert understand_harness.text.requests == []
    content = understand_harness.content_of(understand_harness.image)
    assert content[0] == image_block(fake_image.read_bytes())
    assert content[1:] == [
        {"type": "text", "text": "design notes"},
        {"type": "text", "text": "check"},
    ]


def test_paths_mixing_types_go_to_the_one_model_that_accepts_them_all(
    understand_harness: UnderstandHarness, fake_image: Path, fake_pdf: Path
) -> None:
    understand_harness.understand(
        [{"prompt": "compare", "paths": [str(fake_image), str(fake_pdf)]}]
    )

    content = understand_harness.content_of(understand_harness.pdf)
    assert [block["type"] for block in content] == ["image_url", "media", "text"]
    assert understand_harness.text.requests == []


def test_a_model_not_accepting_its_own_type_is_refused_by_its_setting_before_any_model_is_built(
    understand_harness: UnderstandHarness, fake_image: Path, fake_pdf: Path
) -> None:
    understand_harness.configure_models(image="deepseek-flash", pdf="deepseek-flash")

    with pytest.raises(UnderstandError, match="MEIVA_UNDERSTAND_PDF_MODEL"):
        understand_harness.understand([{"prompt": "x", "paths": [str(fake_image), str(fake_pdf)]}])

    assert understand_harness.pdf.requests == []


def test_paths_all_text_go_to_the_text_model(
    understand_harness: UnderstandHarness, tmp_path: Path
) -> None:
    a = tmp_path / "a.md"
    a.write_text("material A", encoding="utf-8")
    b = tmp_path / "b.md"
    b.write_text("material B", encoding="utf-8")

    [out] = understand_harness.understand([{"prompt": "diff", "paths": [str(a), str(b)]}])

    assert out == "fake answer"
    assert understand_harness.pdf.requests == []
    assert understand_harness.content_of(understand_harness.text) == [
        {"type": "text", "text": "material A"},
        {"type": "text", "text": "material B"},
        {"type": "text", "text": "diff"},
    ]


def test_paths_validation() -> None:
    with pytest.raises(TypeError, match="must be a list of file paths"):
        understand([{"prompt": "p", "paths": "a.png"}])
    with pytest.raises(TypeError, match="must be a list of file paths"):
        understand(cast(Any, [{"prompt": "p", "paths": ("a.png", "b.png")}]))
    with pytest.raises(ValueError, match="must not be empty"):
        understand([{"prompt": "p", "paths": []}])
    with pytest.raises(TypeError, match="must be a path string or Path"):
        understand(cast(Any, [{"prompt": "p", "paths": [123]}]))
    with pytest.raises(ValueError, match="exactly one of 'text' / 'paths'"):
        understand([{"prompt": "p", "path": "a.png"}])
    with pytest.raises(ValueError, match="exactly one of 'text' / 'paths'"):
        understand([{"prompt": "p", "text": "x", "paths": ["b.png"]}])


def test_paths_missing_file_raises(understand_harness: UnderstandHarness, tmp_path: Path) -> None:
    good = tmp_path / "good.png"
    good.write_bytes(b"\x89PNG\r\n\x1a\n")

    with pytest.raises(FileNotFoundError, match="does not name an existing file"):
        understand_harness.understand(
            [{"prompt": "p", "paths": [str(good), str(tmp_path / "missing.png")]}]
        )


def test_paths_auto_save_source_labels_list(
    understand_harness: UnderstandHarness, fake_image: Path, tmp_path: Path
) -> None:
    saved_in = tmp_path / "paths_ws" / ".exec_output"
    understand_harness.save_to(saved_in)
    shot = tmp_path / "shot.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")

    understand_harness.understand([{"prompt": "compare", "paths": [str(fake_image), str(shot)]}])

    [saved] = saved_in.glob("understand_*.txt")
    assert saved.name.startswith("understand_20260101_000000_000000_")
    assert f"# source: paths={[str(fake_image), str(shot)]!r}" in saved.read_text()


def test_signature_is_targets_effort_max_concurrent() -> None:
    import inspect

    sig = inspect.signature(understand)
    assert list(sig.parameters) == ["targets", "effort", "max_concurrent"]
    assert sig.parameters["effort"].default == "max"
    assert sig.parameters["max_concurrent"].default == DEFAULT_BATCH_MAX_CONCURRENT
