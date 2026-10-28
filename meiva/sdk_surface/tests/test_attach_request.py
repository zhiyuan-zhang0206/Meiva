# meiva:note
# `meiva/sdk_surface/pure/media/attach/{attach_model,attach_request}.py` unit tests: what a model
# can receive and which members that gates, the steps of one registration around the read the shell
# makes (the gate and validation, the decision over the file facts), what a stat means and when the
# process registers inside a turn. What a leading `~` means is `base.paths.expand`'s, tested beside
# it. Asserted by value over catalogs and facts the test builds; the registration against a real
# file is covered by `meiva/tests/test_attachment_transport.py`.

from __future__ import annotations

import stat
from dataclasses import replace
from pathlib import Path
from typing import Any, cast

import pytest

from base.agents.context.identity import AgentIdentity, ExternalLease
from base.lm.attach.constants import ATTACH_MAX_FILE_BYTES, ATTACH_MAX_LABEL_CHARS
from base.lm.catalog import ModelCatalog
from base.packages.tests.model_catalog import AddModels
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from meiva.sdk_surface.pure.media.attach.attach_model import (
    media_gated_members,
    modalities_for,
    running_model,
)
from meiva.sdk_surface.pure.media.attach.attach_request import (
    FileFacts,
    decide_media,
    file_facts,
    prepare_media,
    registers_in_turn,
)

_MEDIA_MODEL = "gpt-5.6-sol"
_TEXT_ONLY_MODEL = "deepseek-v4-pro"
_LEASE = ExternalLease(agent_id=9, validate=lambda: 9, config=lambda: None)


def _withdrawn_vision_model(
    catalog: ModelCatalog, add_models: AddModels
) -> tuple[str, ModelCatalog]:
    name = "glm-vision-fixture"
    row = replace(
        catalog.models["glm-5.3"],
        spawnable=False,
        unavailable_fallback="glm-5.3",
        media_types=frozenset({"image"}),
    )
    return name, add_models(catalog, {name: row})


def test_a_media_model_receives_images_and_a_text_only_model_nothing(
    model_catalog: ModelCatalog,
) -> None:
    assert "image" in modalities_for(_MEDIA_MODEL, model_catalog)
    assert modalities_for(_TEXT_ONLY_MODEL, model_catalog) == frozenset()


def test_a_withdrawn_model_is_judged_as_its_fallback(
    model_catalog: ModelCatalog, add_models: AddModels
) -> None:
    name, catalog = _withdrawn_vision_model(model_catalog, add_models)
    assert modalities_for(name, catalog) == modalities_for("glm-5.3", catalog) == frozenset()


def test_only_a_model_that_cannot_receive_media_hides_attach(model_catalog: ModelCatalog) -> None:
    assert media_gated_members(_TEXT_ONLY_MODEL, catalog=model_catalog) == {"meiva.self.attach"}
    assert media_gated_members(_MEDIA_MODEL, catalog=model_catalog) == frozenset()


def test_the_running_model_is_the_configured_one_unless_it_was_withdrawn(
    model_catalog: ModelCatalog, add_models: AddModels
) -> None:
    assert running_model(_MEDIA_MODEL, catalog=model_catalog) == _MEDIA_MODEL
    name, catalog = _withdrawn_vision_model(model_catalog, add_models)
    assert running_model(name, catalog=catalog) == "glm-5.3"


def test_a_prepared_request_carries_the_model_facts_and_the_validated_inputs(
    model_catalog: ModelCatalog,
) -> None:
    request = prepare_media(
        Path("out/result.png"),
        label="render",
        catalog=model_catalog,
        model=_MEDIA_MODEL,
        in_turn=True,
    )
    assert (request.path, request.label, request.model, request.in_turn) == (
        Path("out/result.png"),
        "render",
        _MEDIA_MODEL,
        True,
    )
    assert request.modalities == modalities_for(_MEDIA_MODEL, model_catalog)


def test_a_text_only_model_is_refused_before_the_inputs_are_looked_at(
    model_catalog: ModelCatalog,
) -> None:
    with pytest.raises(RuntimeError, match="text-only"):
        prepare_media(
            cast(Any, 3), label=None, catalog=model_catalog, model=_TEXT_ONLY_MODEL, in_turn=True
        )


def test_a_path_that_is_neither_str_nor_path_and_a_label_that_is_not_a_str_are_refused(
    model_catalog: ModelCatalog,
) -> None:
    with pytest.raises(TypeError, match="path"):
        prepare_media(
            cast(Any, 3), label=None, catalog=model_catalog, model=_MEDIA_MODEL, in_turn=True
        )
    with pytest.raises(TypeError, match="label"):
        prepare_media(
            "a.png", label=cast(Any, 1), catalog=model_catalog, model=_MEDIA_MODEL, in_turn=True
        )


@pytest.mark.parametrize(
    ("mode", "size", "is_file", "kept_size"),
    [
        (stat.S_IFREG | 0o644, 10, True, 10),
        (stat.S_IFDIR | 0o755, 4096, False, 0),
        (None, 0, False, 0),
    ],
)
def test_only_a_regular_file_has_a_size(
    mode: int | None, size: int, is_file: bool, kept_size: int
) -> None:
    facts = file_facts(Path("/x"), mode=mode, size=size)
    assert (facts.is_file, facts.size, facts.resolved) == (is_file, kept_size, Path("/x"))


def _request(
    model_catalog: ModelCatalog,
    *,
    model: str = _MEDIA_MODEL,
    in_turn: bool = True,
    path: str = "result.png",
    label: str | None = None,
):
    return prepare_media(path, label=label, catalog=model_catalog, model=model, in_turn=in_turn)


def _file(name: str = "result.png", *, size: int = 3, is_file: bool = True) -> FileFacts:
    return FileFacts(resolved=Path("/work") / name, is_file=is_file, size=size)


def test_a_registered_file_is_recorded_by_its_resolved_path_and_label(
    model_catalog: ModelCatalog,
) -> None:
    request = _request(model_catalog, label="render")
    assert decide_media(request, _file()) == ("/work/result.png", "render")


def test_a_registration_outside_a_turn_is_refused_first(model_catalog: ModelCatalog) -> None:
    with pytest.raises(RuntimeError, match="only works inside an agent turn"):
        decide_media(_request(model_catalog, in_turn=False), _file(is_file=False))


def test_a_path_that_is_not_a_file_is_not_found(model_catalog: ModelCatalog) -> None:
    with pytest.raises(FileNotFoundError, match="does not name an existing file"):
        decide_media(_request(model_catalog), _file(is_file=False))


def test_a_suffix_the_media_table_does_not_know_is_refused(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match="unsupported attachment suffix"):
        decide_media(_request(model_catalog, path="notes.txt"), _file("notes.txt"))


def test_a_modality_the_model_cannot_receive_is_refused(model_catalog: ModelCatalog) -> None:
    with pytest.raises(ValueError, match=r"modality 'video'.*supported modalities: image"):
        decide_media(_request(model_catalog, path="clip.mp4"), _file("clip.mp4"))


def test_a_file_over_the_limit_and_a_label_over_the_limit_are_refused(
    model_catalog: ModelCatalog,
) -> None:
    with pytest.raises(ValueError, match="20 MiB"):
        decide_media(_request(model_catalog), _file(size=ATTACH_MAX_FILE_BYTES + 1))
    long_label = "x" * (ATTACH_MAX_LABEL_CHARS + 1)
    with pytest.raises(ValueError, match="label exceeds"):
        decide_media(_request(model_catalog, label=long_label), _file())


def test_the_limit_itself_is_accepted(model_catalog: ModelCatalog) -> None:
    request = _request(model_catalog, label="x" * ATTACH_MAX_LABEL_CHARS)
    assert decide_media(request, _file(size=ATTACH_MAX_FILE_BYTES))[0] == "/work/result.png"


@pytest.mark.parametrize(
    ("in_exec_turn", "identity", "expected"),
    [
        (True, None, True),
        (False, None, False),
        (True, AgentIdentity(agent_id=7, owns_loop=True), True),
        (True, AgentIdentity(agent_id=7, owns_loop=False, lease=_LEASE), False),
        (False, AgentIdentity(agent_id=7, owns_loop=True), False),
    ],
)
def test_a_registration_is_inside_a_turn_when_one_runs_and_the_identity_is_not_borrowed(
    in_exec_turn: bool, identity: AgentIdentity | None, expected: bool
) -> None:
    assert registers_in_turn(in_exec_turn=in_exec_turn, identity=identity) is expected
