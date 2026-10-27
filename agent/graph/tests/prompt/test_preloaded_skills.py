# meiva:note
# `skills_to_expand_at_start` and the preloaded-skills note. Two surfaces: the resolver shared
# with the capabilities index (`skill_selection.resolve_prompt_skills`: wildcard,
# identifier-then-name, unresolved names returned for the reader to report) and the full
# SKILL.md note injected at cold start and after every compact (`context_notes`). Skills are
# real folders under `tmp_path`, scanned by the production scanner into the `SkillView` the
# resolver decides on; only the shell tests at the end read the installed catalog, through a
# real install registry in a per-test unit home.
from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import BaseMessage

from agent.graph.prompt.context_notes import (
    fork_tail_skills_note,
    preloaded_note,
    preloaded_skills_note,
)
from agent.graph.prompt.skill_selection import resolve_prompt_skills
from agent.graph.tests.prompt.skill_support import skill_view, write_skill
from base.agents.context import MeivaContext
from base.agents.messages.kwargs import NoteTag
from base.api_contracts.agent_contract import Harness
from base.clock import SYSTEM_TIMERS, Clock, ClockConfig
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.packages.extensions import install_registry
from base.packages.extensions.install_registry import InstalledPackage
from base.packages.skills.tree import Skill, identifier
from base.paths import meiva_home, skills_dir
from meiva.sdk_surface.tests.context_home import context_over_unit_home as context_over_unit_home


def test_resolve_by_bare_name(tmp_path: Path) -> None:
    write_skill(tmp_path, "ultra_speed", "ultra_speed", "go fast")

    resolved = resolve_prompt_skills(["ultra_speed"], skill_view(tmp_path))

    assert [s["name"] for s in resolved.skills] == ["ultra_speed"]
    assert resolved.unresolved == ()


def test_resolve_by_dotted_identifier(tmp_path: Path) -> None:
    """A namespaced skill resolves by its `.`-identifier, not just bare name."""
    write_skill(tmp_path / "meiva-memory", "consolidation", "consolidation", "merge notes")

    resolved = resolve_prompt_skills(["meiva-memory.consolidation"], skill_view(tmp_path))

    assert [identifier(s) for s in resolved.skills] == ["meiva-memory:consolidation"]


def test_resolve_accepts_the_python_spelling_of_a_dash_skill(tmp_path: Path) -> None:
    """A config value still written in the underscore (Python) form resolves to
    the dash-named skill — the backcompat that keeps a preset row written before
    the rename working."""
    write_skill(tmp_path / "meiva-memory", "consolidation", "consolidation", "merge notes")

    resolved = resolve_prompt_skills(["meiva_memory.consolidation"], skill_view(tmp_path))

    assert [identifier(s) for s in resolved.skills] == ["meiva-memory:consolidation"]


def test_resolve_accepts_the_plugin_colon_spelling(tmp_path: Path) -> None:
    """An ecosystem-style `plugin:skill` reference folds to the `.` form."""
    write_skill(tmp_path / "meiva-memory", "consolidation", "consolidation", "merge notes")

    resolved = resolve_prompt_skills(["meiva-memory:consolidation"], skill_view(tmp_path))

    assert [identifier(s) for s in resolved.skills] == ["meiva-memory:consolidation"]


def test_resolve_wildcard_selects_whole_catalog(tmp_path: Path) -> None:
    write_skill(tmp_path, "alpha", "alpha", "a")
    write_skill(tmp_path, "beta", "beta", "b")

    resolved = resolve_prompt_skills(["*"], skill_view(tmp_path))

    assert {s["name"] for s in resolved.skills} == {"alpha", "beta"}


def test_resolve_unknown_name_is_returned_not_selected(tmp_path: Path) -> None:
    write_skill(tmp_path, "real", "real", "r")

    resolved = resolve_prompt_skills(["real", "does_not_exist"], skill_view(tmp_path))

    assert [s["name"] for s in resolved.skills] == ["real"]
    assert resolved.unresolved == ("does_not_exist",)


def test_resolve_keeps_the_requested_order(tmp_path: Path) -> None:
    write_skill(tmp_path, "first", "first", "1")
    write_skill(tmp_path, "second", "second", "2")

    resolved = resolve_prompt_skills(["second", "first"], skill_view(tmp_path))

    assert [s["name"] for s in resolved.skills] == ["second", "first"]


def test_resolve_empty_list_returns_empty(tmp_path: Path) -> None:
    write_skill(tmp_path, "real", "real", "r")

    resolved = resolve_prompt_skills([], skill_view(tmp_path))

    assert (resolved.skills, resolved.unresolved) == ((), ())


def test_resolve_returns_empty_when_skills_sdk_disabled(tmp_path: Path) -> None:
    write_skill(tmp_path, "real", "real", "r")

    resolved = resolve_prompt_skills(["real"], skill_view(tmp_path, disabled=["skills"]))

    assert (resolved.skills, resolved.unresolved) == ((), ())


def _text(note: BaseMessage) -> str:
    content: object = note.content
    assert isinstance(content, str)
    return content


def _skills(root: Path, wanted: list[str]) -> tuple[Skill, ...]:
    return resolve_prompt_skills(wanted, skill_view(root)).skills


def test_note_none_when_nothing_resolves(tmp_path: Path) -> None:
    write_skill(tmp_path, "real", "real", "r")

    assert preloaded_note(_skills(tmp_path, ["ghost"]), harness=Harness.MEIVA) is None


def test_note_carries_full_body_and_tag(tmp_path: Path) -> None:
    body = "# Ultra speed\n\nDo the fast thing. Never dawdle."
    write_skill(tmp_path, "ultra_speed", "ultra_speed", "go fast", body=body)

    note = preloaded_note(_skills(tmp_path, ["ultra_speed"]), harness=Harness.MEIVA)

    assert note is not None
    assert isinstance(note.content, str)  # pyright: ignore[reportUnknownMemberType]
    content = note.content
    assert content.startswith("<system_note>\nPreloaded skills")
    assert note.additional_kwargs["meiva_note_tag"] == NoteTag.PRELOADED_SKILLS.value  # pyright: ignore[reportUnknownMemberType]
    # The access-path heading + the full SKILL.md body (frontmatter included, as
    # meiva.help renders a skill) are both present.
    assert "## meiva.skills.ultra_speed" in content
    assert "Do the fast thing. Never dawdle." in content
    assert "description: go fast" in content  # frontmatter is part of the full text


def test_note_merges_multiple_skills_in_order(tmp_path: Path) -> None:
    write_skill(tmp_path, "first", "first", "1", body="AAA body")
    write_skill(tmp_path, "second", "second", "2", body="BBB body")

    note = preloaded_note(_skills(tmp_path, ["second", "first"]), harness=Harness.MEIVA)

    assert note is not None
    assert isinstance(note.content, str)  # pyright: ignore[reportUnknownMemberType]
    content = note.content
    # One note, both bodies, a `---` separator between the two sections.
    assert "AAA body" in content and "BBB body" in content
    assert "\n---\n" in content
    # Requested order (second before first) is the render order.
    assert content.index("## meiva.skills.second") < content.index("## meiva.skills.first")


def test_note_heading_uses_dotted_access_path(tmp_path: Path) -> None:
    """A namespaced skill's heading is its meiva.skills access path (attr form)."""
    write_skill(
        tmp_path / "meiva_memory",
        "consolidation",
        "consolidation",
        "merge notes",
        body="consolidation playbook",
    )

    note = preloaded_note(_skills(tmp_path, ["meiva_memory.consolidation"]), harness=Harness.MEIVA)

    assert note is not None
    assert isinstance(note.content, str)  # pyright: ignore[reportUnknownMemberType]
    assert "## meiva.skills.meiva_memory.consolidation" in note.content
    assert "consolidation playbook" in note.content


def test_note_for_an_external_harness_points_at_its_native_file_tool(tmp_path: Path) -> None:
    write_skill(tmp_path, "ultra_speed", "ultra_speed", "go fast", body="body")

    note = preloaded_note(_skills(tmp_path, ["ultra_speed"]), harness=Harness.CLAUDE_CODE)

    assert note is not None
    text = _text(note)
    assert "your native file-reading tool" in text
    assert "meiva.files.read" not in text


def test_note_skips_a_skill_whose_body_cannot_be_read(tmp_path: Path) -> None:
    write_skill(tmp_path, "gone", "gone", "g")
    write_skill(tmp_path, "kept", "kept", "k", body="KEPT body")
    skills = _skills(tmp_path, ["gone", "kept"])
    (tmp_path / "gone" / "SKILL.md").unlink()

    note = preloaded_note(skills, harness=Harness.MEIVA)

    assert note is not None
    text = _text(note)
    assert "KEPT body" in text
    assert "meiva.skills.gone" not in text


def _install_skill(name: str, description: str, body: str) -> None:
    root = skills_dir(meiva_home())
    write_skill(root, name, name, description, body=body)
    install_registry.register(InstalledPackage(name=name, type="skill"), home=meiva_home())


def _clock() -> Clock:
    return Clock(ClockConfig("UTC", "UTC", False), timers=SYSTEM_TIMERS)


def _context(**pins: object) -> MeivaContext:
    return MeivaContext(
        meiva_home=meiva_home(),
        agent=AgentSlices.resolve(pins, default_reader=defaults()),
        clock_factory=_clock,
    )


@pytest.mark.usefixtures("unit_home")
def test_installed_skills_are_preloaded_by_the_configured_names() -> None:
    _install_skill("ultra_speed", "go fast", "Do the fast thing.")
    _install_skill("other", "not asked for", "OTHER body")

    note = preloaded_skills_note(_context(skills_to_expand_at_start=["ultra_speed"]))

    assert note is not None
    text = _text(note)
    assert "Do the fast thing." in text
    assert "OTHER body" not in text


@pytest.mark.usefixtures("unit_home")
def test_a_configured_name_that_matches_nothing_warns_and_yields_no_note(
    caplog: pytest.LogCaptureFixture,
) -> None:
    _install_skill("real", "r", "REAL body")

    with caplog.at_level("WARNING"):
        note = preloaded_skills_note(_context(skills_to_expand_at_start=["ghost"]))

    assert note is None
    assert "ghost" in caplog.text
    assert "skills_to_expand_at_start" in caplog.text


@pytest.mark.usefixtures("unit_home")
def test_no_note_without_configured_skills() -> None:
    _install_skill("real", "r", "REAL body")

    assert preloaded_skills_note(_context(skills_to_expand_at_start=[])) is None


@pytest.mark.usefixtures("unit_home")
def test_fork_tail_note_grafts_the_named_installed_skills() -> None:
    _install_skill("extra", "the extra skill", "EXTRA body")

    note = fork_tail_skills_note(["extra"], [], home=meiva_home())

    assert note is not None
    text = _text(note)
    assert text.startswith("<system_note>\nAdditional skills for you")
    assert "## meiva.skills.extra" in text
    assert "EXTRA body" in text


@pytest.mark.usefixtures("unit_home")
def test_fork_tail_note_is_empty_when_the_skills_surface_is_disabled() -> None:
    _install_skill("extra", "the extra skill", "EXTRA body")

    assert fork_tail_skills_note(["extra"], ["skills"], home=meiva_home()) is None
