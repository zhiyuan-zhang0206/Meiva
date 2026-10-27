# meiva:note
# The reads of the running process that the system prompt and the capability hooks make, in one
# place. Everything the prompt decides on from the live `meiva` SDK (the skill catalog, the MCP
# servers, the entries disabled at run time, the plugin-declared expansions, the public
# namespaces) is read here and handed to the pure decisions in `sdk_scope.py`,
# `skill_selection.py` and `capabilities.py` as values. The expanded SDK reference is rendered
# here too because rendering walks the live modules.
from __future__ import annotations

import contextlib
import inspect
import io
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

from base.host.env.agent_slices import AgentSlices, Prompt
from base.lm.catalog import ModelCatalog
from base.packages.skills.tree import Skill

from .capabilities import McpServer
from .sdk_scope import CAPABILITY_SURFACES, disabled_by_sdk_config
from .skill_selection import SkillView, resolve_prompt_skills


@dataclass(frozen=True)
class LiveSdk:
    view: SkillView
    mcp_servers: tuple[McpServer, ...]
    plugin_expansions: tuple[str, ...]


# meiva:note
# Everything a framework system-prompt section can decide on, handed to every section the same
# way: the frozen agent configuration, the model catalog and the live SDK reads. A section takes
# the bundle and ignores the parts it has no use for.
@dataclass(frozen=True)
class SectionInputs:
    slices: AgentSlices
    catalog: ModelCatalog
    live: LiveSdk


def effective_sdk_disable(configured: Sequence[str]) -> tuple[str, ...]:
    from meiva.sdk_surface import sdk_disable

    return (*configured, *sorted(sdk_disable.applied_entries()))


def warn_unresolved(config_field: str, names: Sequence[str]) -> None:
    for name in names:
        logging.getLogger(__name__).warning(
            "%s: skill %r not found among loaded skills, skipping", config_field, name
        )


def read_skills(disabled: Sequence[str], *, home: Path) -> tuple[Skill, ...]:
    if disabled_by_sdk_config("skills", disabled):
        return ()
    from meiva.skills import names_under

    return tuple(names_under(home))


def read_view(
    inject: tuple[str, ...],
    expand: tuple[str, ...],
    configured_disable: Sequence[str],
    *,
    home: Path,
) -> SkillView:
    disabled = effective_sdk_disable(configured_disable)
    return SkillView(
        inject, expand, disabled, read_skills(disabled, home=home) if inject or expand else ()
    )


def skill_view(prompt: Prompt, *, home: Path) -> SkillView:
    view = read_view(
        prompt.skills_to_inject_into_system_prompt,
        prompt.skills_to_expand_at_start,
        prompt.sdk_disable,
        home=home,
    )
    warn_unresolved(
        "skills_to_inject_into_system_prompt", resolve_prompt_skills(view.inject, view).unresolved
    )
    return view


def read_mcp_servers(disabled: Sequence[str], *, home: Path) -> tuple[McpServer, ...]:
    if disabled_by_sdk_config("mcps", disabled):
        return ()
    from base.packages.mcp_config import load_mcp_config

    configured = load_mcp_config(home=home)
    return tuple(
        McpServer(name, configured[name].get("description")) for name in sorted(configured)
    )


def read_plugin_expansions() -> tuple[str, ...]:
    from meiva.sdk_surface import install

    return install.expansions()


def read_live_sdk(prompt: Prompt, *, home: Path) -> LiveSdk:
    view = skill_view(prompt, home=home)
    return LiveSdk(view, read_mcp_servers(view.disabled, home=home), read_plugin_expansions())


def discover_all_namespaces(disabled: Sequence[str]) -> list[str]:
    import meiva

    discovered: list[str] = []

    def _collect(parent: ModuleType, prefix: str) -> None:
        for name in meiva.agent_visible_names(parent):
            full = f"{prefix}.{name}" if prefix else name
            if full in CAPABILITY_SURFACES or disabled_by_sdk_config(full, disabled):
                continue
            attr = getattr(parent, name, None)
            if inspect.ismodule(attr) or isinstance(attr, SimpleNamespace):
                discovered.append(full)

                if inspect.ismodule(attr) and hasattr(attr, "__all_for_meiva__"):
                    _collect(attr, full)

    _collect(meiva, "")
    return sorted(discovered)


def render_expanded_reference(
    paths: Sequence[str],
    *,
    disabled: Sequence[str],
    model: str,
    catalog: ModelCatalog,
    report: bool,
    extra_hidden_members: frozenset[str] = frozenset(),
) -> str:
    import meiva
    from meiva.sdk_surface.help import ModelHelpProfile
    from meiva.sdk_surface.help import render as render_help

    base_profile = ModelHelpProfile.for_model(model, catalog=catalog)
    profile = (
        ModelHelpProfile(
            hidden_members=base_profile.hidden_members | extra_hidden_members,
            doc_style=base_profile.doc_style,
        )
        if extra_hidden_members
        else base_profile
    )

    logger = logging.getLogger(__name__)
    pieces: list[str] = []
    seen_targets: set[int] = set()
    for path in paths:
        if disabled_by_sdk_config(path, disabled):
            continue
        target: object = meiva
        try:
            for segment in path.split("."):
                target = getattr(target, segment)
        except AttributeError:
            if report:
                logger.warning(
                    "sdk_expand_in_system_prompt: meiva.%s does not resolve and is not covered "
                    "by MEIVA_SDK_DISABLE (typo, or a plugin namespace whose plugin is not "
                    "loaded?), skipping",
                    path,
                )
            continue
        if id(target) in seen_targets:
            if report:
                logger.warning(
                    "sdk_expand_in_system_prompt: %r resolves to an already-expanded "
                    "namespace; rendering once. The expand list should be deduped, so a "
                    "duplicate points at a polluted list (a stray plugin sdk expansion or "
                    "cross-test global-state leak). effective list was %r",
                    path,
                    list(paths),
                )
            continue
        seen_targets.add(id(target))
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            render_help(target, compact_classes=True, profile=profile)
        pieces.append(buf.getvalue().rstrip())
    if not pieces:
        return ""
    body = "\n\n".join(pieces)
    return f"# Expanded SDK reference\n\nFull contracts for your most-used namespaces.\n\n{body}"
