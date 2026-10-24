# meiva:note
# `meiva.self.attach` registration: the explicit-input body (`plan_media` / `record_media`)
# takes the catalog, the agent's model, the workspace directory and the turn as values; one
# facade test exercises the process reads (`register`) against a held installation and a real
# exec-turn slot.


from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from base.agents.sdk.tests.sampling_owners import fixed_sampling_owner
from base.config.service_read import ConfigAuthority
from base.config.tests.configuration import snapshot_process_config
from base.lm.attach.constants import ATTACH_MAX_FILE_BYTES, ATTACH_MAX_LABEL_CHARS
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import AddModels
from meiva.sdk_surface import process_context
from meiva.sdk_surface.attachment_transport import plan_media, record_media, register
from meiva.sdk_surface.installation import Installation
from meiva.sdk_surface.tests.installation_holder import held_installation

_MEDIA_MODEL = "gpt-5.6-sol"


def _plan(
    path: str | Path,
    base: Path,
    catalog: ModelCatalog,
    *,
    label: str | None = None,
    model: str = _MEDIA_MODEL,
    in_turn: bool = True,
) -> tuple[str, str | None]:
    return plan_media(path, label=label, base=base, catalog=catalog, model=model, in_turn=in_turn)


def test_registers_resolved_path_into_the_turn_update(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"png")
    update: dict[str, Any] = {}

    record_media(update, _plan(image, tmp_path, model_catalog, label="render result"))
    record_media(update, _plan(image, tmp_path, model_catalog))

    assert update["attach"] == [
        {"path": str(image.resolve()), "label": "render result"},
        {"path": str(image.resolve()), "label": None},
    ]


def test_rejects_calls_outside_an_agent_turn(tmp_path: Path, model_catalog: ModelCatalog) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="only works inside an agent turn"):
        _plan(image, tmp_path, model_catalog, in_turn=False)


def test_validates_path_suffix_size_and_label(tmp_path: Path, model_catalog: ModelCatalog) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    text_file = tmp_path / "notes.txt"
    text_file.write_text("text", encoding="utf-8")
    oversized = tmp_path / "large.png"
    with oversized.open("wb") as stream:
        stream.truncate(ATTACH_MAX_FILE_BYTES + 1)
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(FileNotFoundError):
        _plan(tmp_path / "missing.png", tmp_path, model_catalog)
    with pytest.raises(FileNotFoundError):
        _plan(directory, tmp_path, model_catalog)
    with pytest.raises(ValueError, match=r"text files belong in exec output or meiva.understand"):
        _plan(text_file, tmp_path, model_catalog)
    with pytest.raises(ValueError, match="20 MiB"):
        _plan(oversized, tmp_path, model_catalog)
    with pytest.raises(TypeError, match="label"):
        _plan(image, tmp_path, model_catalog, label=cast(Any, 1))
    with pytest.raises(ValueError, match="120"):
        _plan(image, tmp_path, model_catalog, label="x" * (ATTACH_MAX_LABEL_CHARS + 1))


def test_a_relative_path_resolves_against_the_workspace_base(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    image = tmp_path / "relative.png"
    image.write_bytes(b"png")

    assert _plan("relative.png", tmp_path, model_catalog) == (str(image.resolve()), None)


def test_rejects_text_only_model(tmp_path: Path, model_catalog: ModelCatalog) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="text-only"):
        _plan(image, tmp_path, model_catalog, model="deepseek-v4-pro")


def test_rejects_model_withdrawn_to_its_text_only_fallback(
    tmp_path: Path, add_models: AddModels, model_catalog: ModelCatalog
) -> None:
    model = "glm-vision-fixture"
    catalog = add_models(
        model_catalog,
        {
            model: replace(
                model_catalog.models["glm-5.3"],
                spawnable=False,
                unavailable_fallback="glm-5.3",
                media_types=frozenset({"image"}),
            )
        },
    )
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="text-only"):
        _plan(image, tmp_path, catalog, model=model)


def test_rejects_modality_not_supported_by_model(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")

    with pytest.raises(ValueError, match=r"modality 'video'.*supported modalities: image"):
        _plan(video, tmp_path, model_catalog)


def test_attach_modality_matrix_follows_registry(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    assert _plan(video, tmp_path, model_catalog, model="gemini-3.8-flash") == (
        str(video.resolve()),
        None,
    )
    assert _plan(image, tmp_path, model_catalog, model="gemini-3.8-flash") == (
        str(image.resolve()),
        None,
    )
    with pytest.raises(ValueError, match=r"modality 'video'.*supported modalities: image, pdf"):
        _plan(video, tmp_path, model_catalog, model="claude-sonnet-5")
    assert _plan(image, tmp_path, model_catalog, model="claude-sonnet-5") == (
        str(image.resolve()),
        None,
    )


@pytest.fixture
def exec_turn() -> Iterator[dict[str, Any]]:
    update: dict[str, Any] = {}
    process_context.set_state(object())
    process_context.set_state_update(update)
    yield update
    process_context.unbind_exec_turn()


# meiva:note
# The facade reads the held installation's catalog, the agent's model from its configuration
# authority and the exec-turn slot, then registers: one registration lands in the turn's update.
def test_register_reads_the_process_and_records_into_the_turn(
    tmp_path: Path,
    model_catalog: ModelCatalog,
    exec_turn: dict[str, Any],
    unit_home: Path,
) -> None:
    owner = snapshot_process_config()
    owner.set_field("llm_model", _MEDIA_MODEL)
    authority = ConfigAuthority(owner.view, owner.view, unit_home / ".env")
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with held_installation(
        Installation.bare(
            catalog=model_catalog, authority=authority, sampling=fixed_sampling_owner()
        )
    ):
        register(image, label="render result", base=tmp_path)

    assert exec_turn["attach"] == [{"path": str(image.resolve()), "label": "render result"}]
