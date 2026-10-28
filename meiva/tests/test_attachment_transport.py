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
_TEXT_ONLY_MODEL = "deepseek-v4-pro"


def _plan(
    path: str | Path,
    base: Path,
    catalog: ModelCatalog,
    *,
    user_home: Path,
    label: str | None = None,
    model: str = _MEDIA_MODEL,
    in_turn: bool = True,
) -> tuple[str, str | None]:
    return plan_media(
        path,
        label=label,
        base=base,
        user_home=user_home,
        catalog=catalog,
        model=model,
        in_turn=in_turn,
    )


def test_registers_resolved_path_into_the_turn_update(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"png")
    update: dict[str, Any] = {}

    record_media(
        update, _plan(image, tmp_path, model_catalog, user_home=tmp_path, label="render result")
    )
    record_media(update, _plan(image, tmp_path, model_catalog, user_home=tmp_path))

    assert update["attach"] == [
        {"path": str(image.resolve()), "label": "render result"},
        {"path": str(image.resolve()), "label": None},
    ]


def test_rejects_calls_outside_an_agent_turn(tmp_path: Path, model_catalog: ModelCatalog) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="only works inside an agent turn"):
        _plan(image, tmp_path, model_catalog, user_home=tmp_path, in_turn=False)


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
        _plan(tmp_path / "missing.png", tmp_path, model_catalog, user_home=tmp_path)
    with pytest.raises(FileNotFoundError):
        _plan(directory, tmp_path, model_catalog, user_home=tmp_path)
    with pytest.raises(ValueError, match=r"text files belong in exec output or meiva.understand"):
        _plan(text_file, tmp_path, model_catalog, user_home=tmp_path)
    with pytest.raises(ValueError, match="20 MiB"):
        _plan(oversized, tmp_path, model_catalog, user_home=tmp_path)
    with pytest.raises(TypeError, match="label"):
        _plan(image, tmp_path, model_catalog, user_home=tmp_path, label=cast(Any, 1))
    with pytest.raises(ValueError, match="120"):
        _plan(
            image,
            tmp_path,
            model_catalog,
            user_home=tmp_path,
            label="x" * (ATTACH_MAX_LABEL_CHARS + 1),
        )


def test_a_relative_path_resolves_against_the_workspace_base(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    image = tmp_path / "relative.png"
    image.write_bytes(b"png")

    assert _plan("relative.png", tmp_path, model_catalog, user_home=tmp_path) == (
        str(image.resolve()),
        None,
    )


def test_rejects_text_only_model(tmp_path: Path, model_catalog: ModelCatalog) -> None:
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with pytest.raises(RuntimeError, match="text-only"):
        _plan(image, tmp_path, model_catalog, user_home=tmp_path, model="deepseek-v4-pro")


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
        _plan(image, tmp_path, catalog, user_home=tmp_path, model=model)


def test_rejects_modality_not_supported_by_model(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")

    with pytest.raises(ValueError, match=r"modality 'video'.*supported modalities: image"):
        _plan(video, tmp_path, model_catalog, user_home=tmp_path)


def test_attach_modality_matrix_follows_registry(
    tmp_path: Path, model_catalog: ModelCatalog
) -> None:
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"mp4")
    image = tmp_path / "shot.png"
    image.write_bytes(b"png")

    assert _plan(video, tmp_path, model_catalog, user_home=tmp_path, model="gemini-3.8-flash") == (
        str(video.resolve()),
        None,
    )
    assert _plan(image, tmp_path, model_catalog, user_home=tmp_path, model="gemini-3.8-flash") == (
        str(image.resolve()),
        None,
    )
    with pytest.raises(ValueError, match=r"modality 'video'.*supported modalities: image, pdf"):
        _plan(video, tmp_path, model_catalog, user_home=tmp_path, model="claude-sonnet-5")
    assert _plan(image, tmp_path, model_catalog, user_home=tmp_path, model="claude-sonnet-5") == (
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
        register(image, label="render result", base=tmp_path, user_home=tmp_path)

    assert exec_turn["attach"] == [{"path": str(image.resolve()), "label": "render result"}]


# meiva:note
# `register` hands the model its settings configure to the gate: a text-only configuration refuses
# the registration and names that model, so a hard-coded or defaulted model would pass silently.
def test_register_gates_on_the_model_its_settings_configure(
    tmp_path: Path,
    model_catalog: ModelCatalog,
    exec_turn: dict[str, Any],
    unit_home: Path,
) -> None:
    owner = snapshot_process_config()
    owner.set_field("llm_model", _TEXT_ONLY_MODEL)
    authority = ConfigAuthority(owner.view, owner.view, unit_home / ".env")
    image = tmp_path / "result.png"
    image.write_bytes(b"png")

    with (
        held_installation(
            Installation.bare(
                catalog=model_catalog, authority=authority, sampling=fixed_sampling_owner()
            )
        ),
        pytest.raises(RuntimeError, match=f"{_TEXT_ONLY_MODEL}.*text-only"),
    ):
        register(image, label=None, base=tmp_path, user_home=tmp_path)

    assert exec_turn == {}


# meiva:note
# `meiva.self.attach` resolves a relative path under the workspace of the bound context's Meiva
# home. The context here names a home that differs from the environment's, so a base read from
# the environment would look for the file under `unit_home` and not find it.
def test_attach_resolves_a_relative_path_under_the_workspace_of_the_contexts_home(
    tmp_path: Path,
    model_catalog: ModelCatalog,
    exec_turn: dict[str, Any],
    unit_home: Path,
) -> None:
    from meiva.self import attach
    from meiva.tests.fixtures.pin_agent import pin_agent

    owner = snapshot_process_config()
    owner.set_field("llm_model", _MEDIA_MODEL)
    authority = ConfigAuthority(owner.view, owner.view, unit_home / ".env")
    context_home = tmp_path / "context-home"
    user_home = tmp_path / "user-home"
    image = context_home / "workspaces" / "7" / "result.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    pin_agent(7)
    bound = process_context.current_context()
    process_context.unbind_context()
    process_context.bind_context(replace(bound, meiva_home=context_home, user_home=user_home))
    assert context_home != unit_home
    assert user_home != Path.home()

    with held_installation(
        Installation.bare(
            catalog=model_catalog, authority=authority, sampling=fixed_sampling_owner()
        )
    ):
        attach("result.png")

    assert exec_turn["attach"] == [{"path": str(image.resolve()), "label": None}]


# meiva:note
# `meiva.self.attach` expands a leading `~` against the user home the bound context carries, not
# the process's own: the context here names a home under the test's directory, and the file exists
# only there, so an expansion through the environment would not find it.
def test_attach_expands_a_tilde_path_against_the_contexts_user_home(
    tmp_path: Path,
    model_catalog: ModelCatalog,
    exec_turn: dict[str, Any],
    unit_home: Path,
) -> None:
    from meiva.self import attach
    from meiva.tests.fixtures.pin_agent import pin_agent

    owner = snapshot_process_config()
    owner.set_field("llm_model", _MEDIA_MODEL)
    authority = ConfigAuthority(owner.view, owner.view, unit_home / ".env")
    context_home = tmp_path / "context-home"
    user_home = tmp_path / "user-home"
    image = user_home / "delivered" / "result.png"
    image.parent.mkdir(parents=True)
    image.write_bytes(b"png")
    pin_agent(7)
    bound = process_context.current_context()
    process_context.unbind_context()
    process_context.bind_context(replace(bound, meiva_home=context_home, user_home=user_home))
    assert user_home != Path.home()

    with held_installation(
        Installation.bare(
            catalog=model_catalog, authority=authority, sampling=fixed_sampling_owner()
        )
    ):
        attach("~/delivered/result.png")

    assert exec_turn["attach"] == [{"path": str(image.resolve()), "label": None}]


# meiva:note
# `register` reads the process's own turn and identity: outside an exec turn there is no runner to
# deliver the attachment, and a borrowed lease never runs a turn of its own, so both are refused
# before anything is recorded. The pure rule is `registers_in_turn`; this pins the wiring of the
# two facts the shell hands it.
def test_register_in_a_turn_it_does_not_run_is_refused(
    tmp_path: Path,
    model_catalog: ModelCatalog,
    unit_home: Path,
) -> None:
    from meiva.tests.fixtures.pin_agent import pin_agent

    owner = snapshot_process_config()
    owner.set_field("llm_model", _MEDIA_MODEL)
    authority = ConfigAuthority(owner.view, owner.view, unit_home / ".env")
    image = tmp_path / "result.png"
    image.write_bytes(b"png")
    pin_agent(7)
    process_context.unbind_exec_turn()

    with (
        held_installation(
            Installation.bare(
                catalog=model_catalog, authority=authority, sampling=fixed_sampling_owner()
            )
        ),
        pytest.raises(RuntimeError, match="only works inside an agent turn"),
    ):
        register(image, label=None, base=tmp_path, user_home=tmp_path)


# meiva:note
# The same refusal for an exec turn whose identity is borrowed through an external lease: the
# lease holder's runner delivers, not this process.
def test_register_refuses_a_turn_borrowed_through_a_lease(
    tmp_path: Path,
    model_catalog: ModelCatalog,
    exec_turn: dict[str, Any],
    unit_home: Path,
) -> None:
    from base.agents.context.identity import ExternalLease
    from meiva.tests.fixtures.pin_agent import pin_agent

    owner = snapshot_process_config()
    owner.set_field("llm_model", _MEDIA_MODEL)
    authority = ConfigAuthority(owner.view, owner.view, unit_home / ".env")
    image = tmp_path / "result.png"
    image.write_bytes(b"png")
    pin_agent(7, lease=ExternalLease(agent_id=9, validate=lambda: 9, config=lambda: None))

    with (
        held_installation(
            Installation.bare(
                catalog=model_catalog, authority=authority, sampling=fixed_sampling_owner()
            )
        ),
        pytest.raises(RuntimeError, match="only works inside an agent turn"),
    ):
        register(image, label=None, base=tmp_path, user_home=tmp_path)

    assert exec_turn == {}
