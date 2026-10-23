"""Tests for the system-prompt SDK-expand wildcard (`MEIVA_SDK_EXPAND="*"`).

`effective_sdk_expand()` turns the configured expand list into the concrete
paths the "# Expanded SDK reference" section renders. The `"*"` entry stands for
every top-level public meiva namespace, discovered live from `help(meiva)` so a new
namespace is covered without editing that override. These tests pin: what `"*"`
discovers (and what it deliberately skips — top-level functions, private names,
MEIVA_SDK_DISABLE entries, and the capability surfaces `skills` / `mcps` that the
`# Capabilities` section indexes), how it merges with explicit and
plugin-declared entries, that the legacy explicit-list format is untouched,
that the rendered section reflects the wildcard, and that the field default is
the stable production P95 module set.
"""

from collections.abc import Callable
from typing import Any, cast

import pytest

from agent.graph.prompt.live import LiveSdk, discover_all_namespaces
from agent.graph.prompt.sdk_scope import CAPABILITY_SURFACES
from agent.graph.prompt.skill_selection import SkillView
from agent.graph.prompt.system_prompt import effective_sdk_expand, sdk_expand_section
from agent.graph.tests.prompt.skill_support import write_skill
from base.config import FIELD_INFOS, AgentSettings
from base.config.tests.configuration import defaults
from base.host.env.agent_slices import AgentSlices
from base.lm.catalog import ModelCatalog
from base.packages.extensions import install_registry
from base.packages.extensions.install_registry import InstalledPackage
from base.paths import meiva_home, skills_dir

# The framework-owned top-level namespaces the wildcard must always surface.
# Asserted as a subset (not equality) so a plugin namespace installed into
# `meiva.__all_for_meiva__` during the session does not make these tests brittle.
# `skills` / `mcps` are deliberately absent — they are capability surfaces, not
# SDK API (see test_wildcard_skips_capability_surfaces).
FRAMEWORK_NAMESPACES = {
    "agents",
    "files",
    "self",
    "shell",
    "ui",
    "watcher",
    "web",
}


def _live(disabled: tuple[str, ...] = ()) -> LiveSdk:
    return LiveSdk(SkillView(("*",), (), disabled, ()), (), ())


def _slices(expand: list[str], **changes: Any) -> AgentSlices:
    return AgentSlices.resolve(
        default_reader=defaults(agent__sdk_expand_in_system_prompt=expand, **changes)
    )


def _expand(configured: list[str], *, disabled: tuple[str, ...] = ()) -> list[str]:
    return effective_sdk_expand(configured, disabled=disabled, plugin_expansions=())


def test_wildcard_expands_all_public_namespaces() -> None:
    """`["*"]` resolves to exactly the discovered namespaces — every
    framework namespace, in deterministic sorted order, and nothing the
    discovery would not surface."""
    result = _expand(["*"])

    assert set(result) >= FRAMEWORK_NAMESPACES
    assert "shell.sessions" in result  # nested namespace discovered recursively
    assert result == discover_all_namespaces(())
    assert result == sorted(result)  # deterministic order


def test_wildcard_excludes_functions_and_private_names(model_catalog: ModelCatalog) -> None:
    """Top-level functions (`help`, `understand`) and private names are not
    namespaces — the SDK overview already prints functions in full, so the
    wildcard skips them rather than duplicating their contract."""
    result = _expand(["*"])

    assert "help" not in result
    assert "understand" not in result
    assert "impersonation" not in result
    assert "## meiva.impersonation" not in sdk_expand_section(
        _slices(["*"]), catalog=model_catalog, live=_live()
    )
    assert not any(name.startswith("_") for name in result)


def test_wildcard_skips_capability_surfaces(model_catalog: ModelCatalog) -> None:
    """`skills` and `mcps` are capability surfaces, not SDK API: expanding them
    renders a full listing of every installed skill / configured server, which
    is exactly what `# Capabilities` already indexes. `"*"` skips both so the
    prompt carries one index, not two."""
    result = _expand(["*"])

    assert {"skills", "mcps"} == set(CAPABILITY_SURFACES)
    assert not (set(CAPABILITY_SURFACES) & set(result))
    text = sdk_expand_section(_slices(["*"]), catalog=model_catalog, live=_live())
    assert "## meiva.skills" not in text
    assert "## meiva.mcps" not in text


@pytest.mark.usefixtures("unit_home")
def test_capability_surface_expands_when_named_explicitly(model_catalog: ModelCatalog) -> None:
    """The skip is a wildcard default, not a ban — an operator who names a
    capability SURFACE explicitly still gets it expanded. Expanding the surface
    is index-style, so it costs a duplicate index and nothing worse: no SKILL.md
    body reaches the prompt and no attribution is recorded."""
    write_skill(
        skills_dir(meiva_home()), "alpha", "alpha", "Alpha desc", body="# Body\n\nSECRET_MARKER\n"
    )
    install_registry.register(InstalledPackage(name="alpha", type="skill"))
    assert "skills" in _expand(["*", "skills"])

    text = sdk_expand_section(_slices(["*", "skills"]), catalog=model_catalog, live=_live())

    assert "## meiva.skills" in text
    assert "SECRET_MARKER" not in text
    # An index render carries descriptions, never bodies. Every SKILL.md in this
    # repo opens with a markdown heading; none may appear under the skills block.
    skills_block = text[text.index("## meiva.skills") :]
    skills_block = skills_block.split("\n## meiva.")[0]
    assert "\n# " not in skills_block


def test_member_of_a_capability_surface_is_refused(
    model_catalog: ModelCatalog, caplog: pytest.LogCaptureFixture
) -> None:
    """`skills.<name>` / `mcps.<server>` are refused even when named explicitly.

    Resolving one walks plain getattr down to a single skill / server, which is
    the "agent opened this" path: it would inline that skill's whole SKILL.md
    into every prompt and record a depth="loaded" attribution for a skill nobody
    read. A skill body belongs in skills_to_expand_at_start, which is accounted
    for as prompt cost.

    The refusal lives in `effective_sdk_expand`, so it holds for every consumer
    of that view (the meiva_code section reads it too), not only for the section
    that renders it."""
    configured = ["*", "skills.gmail", "mcps.chrome"]

    with caplog.at_level("WARNING", logger="agent.graph.prompt.system_prompt"):
        result = _expand(configured)

    assert "skills.gmail" not in result
    assert "mcps.chrome" not in result
    assert "refusing 'skills.gmail'" in caplog.text
    assert "refusing 'mcps.chrome'" in caplog.text
    text = sdk_expand_section(_slices(configured), catalog=model_catalog, live=_live())
    assert "## meiva.skills.gmail" not in text
    assert "## meiva.mcps.chrome" not in text


def test_wildcard_respects_sdk_disable() -> None:
    """A namespace listed in MEIVA_SDK_DISABLE is dropped from the wildcard set —
    a disabled namespace must never be expanded back into the prompt."""
    result = _expand(["*"], disabled=("watcher",))

    assert "watcher" not in result
    assert "files" in result and "self" in result


def test_wildcard_disable_of_member_keeps_namespace() -> None:
    """Disabling a member (`self.terminate`) removes only that attribute; the
    parent namespace still expands under the wildcard."""
    assert "self" in _expand(["*"], disabled=("self.terminate",))


def test_wildcard_merges_with_explicit_nested_path() -> None:
    """`["*", "shell.sessions"]` is a union: the wildcard expands every
    top-level and nested namespace; the explicit `shell.sessions` is deduped
    (keep-first from the wildcard block), so `shell` sorts before `shell.sessions`."""
    result = _expand(["*", "shell.sessions"])

    assert "shell.sessions" in result
    assert set(result) >= FRAMEWORK_NAMESPACES
    assert result.index("shell") < result.index("shell.sessions")


def test_wildcard_dedups_overlapping_explicit_entry() -> None:
    """A namespace named explicitly AND discovered by `*` appears once, keeping
    its first (explicit) position."""
    result = _expand(["files", "*"])

    assert result.count("files") == 1
    assert result[0] == "files"


def test_legacy_explicit_list_is_unchanged() -> None:
    """A list with no `"*"` is the old behavior: returned verbatim, deduped
    keep-first, no discovery."""
    assert _expand(["files", "shell", "self"]) == ["files", "shell", "self"]


def test_missing_unregistered_expand_path_warns(
    caplog: pytest.LogCaptureFixture, model_catalog: ModelCatalog
) -> None:
    """An unregistered missing path reaches the existing resolution warning."""
    with caplog.at_level("WARNING", logger="agent.graph.prompt.live"):
        text = sdk_expand_section(
            _slices(["missing_sdk_namespace"]), catalog=model_catalog, live=_live()
        )

    assert text == ""
    assert "meiva.missing_sdk_namespace does not resolve" in caplog.text


def test_disabled_expand_path_is_skipped_without_a_warning(
    caplog: pytest.LogCaptureFixture, model_catalog: ModelCatalog
) -> None:
    with caplog.at_level("WARNING", logger="agent.graph.prompt.live"):
        text = sdk_expand_section(
            _slices(["missing_sdk_namespace"]),
            catalog=model_catalog,
            live=_live(("missing_sdk_namespace",)),
        )

    assert text == ""
    assert caplog.text == ""


def test_plugin_expansions_lead_the_wildcard() -> None:
    """Plugin-promoted paths keep their lead position ahead of the discovered
    set even when the configured list is just `["*"]`."""
    result = effective_sdk_expand(["*"], disabled=(), plugin_expansions=("cwd",))

    assert result[0] == "cwd"
    assert set(result) >= FRAMEWORK_NAMESPACES


def test_section_renders_all_wildcard_namespaces(model_catalog: ModelCatalog) -> None:
    """The rendered section carries one `## meiva.<name>` contract per discovered
    namespace and no heading for the skipped top-level functions."""
    text = sdk_expand_section(_slices(["*"]), catalog=model_catalog, live=_live())

    assert text.startswith("# Expanded SDK reference")
    for ns in FRAMEWORK_NAMESPACES:
        assert f"## meiva.{ns}" in text
    assert "## meiva.understand" not in text
    assert "## meiva.help" not in text
    assert "## meiva.shell.sessions" in text  # nested namespace rendered


def test_field_default_is_production_p95() -> None:
    """The default covers the selected production P95 modules; `*` is opt-in."""
    factory = FIELD_INFOS["sdk_expand_in_system_prompt"].default_factory
    assert factory is not None
    assert factory() == ["shell", "files", "agents", "tasks"]  # type: ignore[call-arg]


def test_plugin_expansions_merge_with_p95_without_duplicates() -> None:
    factory = FIELD_INFOS["sdk_expand_in_system_prompt"].default_factory
    assert factory is not None

    defaults_list = cast("Callable[[], list[str]]", factory)()
    assert effective_sdk_expand(
        defaults_list,
        disabled=(),
        plugin_expansions=("cwd", "tasks"),
    ) == ["cwd", "tasks", "shell", "files", "agents"]


def test_env_comma_string_with_wildcard_parses() -> None:
    """The env form (a comma string) splits into entries; `"*"` survives as its
    own entry and combines with an explicit nested path."""
    s = AgentSettings(MEIVA_SDK_EXPAND="*,shell.sessions")  # pyright: ignore[reportArgumentType]  # env-form str; validator splits to list
    assert s.sdk_expand_in_system_prompt == ["*", "shell.sessions"]


def test_section_hides_attach_for_text_only_model(model_catalog: ModelCatalog) -> None:
    """A text-only model's expanded SDK reference carries no attach contract —
    the member is unavailable to it (user ruling 2026-08-28)."""
    text = sdk_expand_section(
        _slices(["*"], lm__llm_model="deepseek-v4-pro"), catalog=model_catalog, live=_live()
    )

    assert "## meiva.self" in text
    assert "def attach(" not in text


def test_section_keeps_attach_for_media_capable_model(model_catalog: ModelCatalog) -> None:
    """A media-capable model's expanded SDK reference keeps the attach
    contract unchanged (user ruling 2026-08-28)."""
    text = sdk_expand_section(
        _slices(["*"], lm__llm_model="claude-sonnet-5"), catalog=model_catalog, live=_live()
    )

    assert "## meiva.self" in text
    assert "def attach(" in text
