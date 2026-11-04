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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType, SimpleNamespace

from base.host.env.agent_slices import AgentSlices, Prompt
from base.lm.catalog import ModelCatalog
from base.packages.skills.tree import Skill

from .capabilities import McpServer
from .sdk_scope import (
    CAPABILITY_SURFACES,
    ExpandSkip,
    SkippedExpand,
    disabled_by_sdk_config,
    expand_plan,
)
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


def read_skills(
    disabled: Sequence[str], *, home: Path, environ: Mapping[str, str]
) -> tuple[Skill, ...]:
    if disabled_by_sdk_config("skills", disabled):
        return ()
    from meiva.skills import names_under

    return tuple(names_under(home, environ=environ))


def read_view(
    inject: tuple[str, ...],
    expand: tuple[str, ...],
    configured_disable: Sequence[str],
    *,
    home: Path,
    environ: Mapping[str, str],
) -> SkillView:
    disabled = effective_sdk_disable(configured_disable)
    return SkillView(
        inject,
        expand,
        disabled,
        read_skills(disabled, home=home, environ=environ) if inject or expand else (),
    )


def skill_view(prompt: Prompt, *, home: Path, environ: Mapping[str, str]) -> SkillView:
    view = read_view(
        prompt.skills_to_inject_into_system_prompt,
        prompt.skills_to_expand_at_start,
        prompt.sdk_disable,
        home=home,
        environ=environ,
    )
    warn_unresolved(
        "skills_to_inject_into_system_prompt", resolve_prompt_skills(view.inject, view).unresolved
    )
    return view


# meiva:note
# The `description` of the configured MCP server `server`: None when it gave none, and a `TypeError` naming
# the server when it gave a non-string (the same reading as `meiva.mcps`).
def _description(server: str, spec: Mapping[str, object]) -> str | None:
    description = spec.get("description")
    if description is None:
        return None
    if not isinstance(description, str):
        raise TypeError(f"{server}: description must be a string, got {type(description).__name__}")
    return description


def read_mcp_servers(disabled: Sequence[str], *, home: Path) -> tuple[McpServer, ...]:
    if disabled_by_sdk_config("mcps", disabled):
        return ()
    from base.packages.mcp_config import load_mcp_config

    configured = load_mcp_config(home=home)
    return tuple(
        McpServer(name, _description(name, configured[name])) for name in sorted(configured)
    )


# meiva:note
# The docstring of `meiva.self.compact`, the SDK-owned contract a summary model that cannot call
# tools is shown. Imported on call so that building a prompt does not make the `self` namespace
# resident.
def read_compact_doc() -> str | None:
    from meiva.self import compact

    return compact.__doc__


def read_plugin_expansions() -> tuple[str, ...]:
    from meiva.sdk_surface import install

    return install.expansions()


def read_live_sdk(prompt: Prompt, *, home: Path, environ: Mapping[str, str]) -> LiveSdk:
    view = skill_view(prompt, home=home, environ=environ)
    return LiveSdk(view, read_mcp_servers(view.disabled, home=home), read_plugin_expansions())


# meiva:note
# What a configured expand path resolves to, in the two facts its callers need: the object to
# render, or the reason there is nothing to render. The renderer and the context note that names
# the path back to the agent both ask here, so the reference cannot drop an entry the note calls
# fine (or call one fine that never rendered). A first segment missing from this process is
# `UNRESOLVED` rather than a typo: the same read fails for a plugin whose namespace this process
# does not serve.
def read_expand_entry(path: str) -> tuple[object | None, ExpandSkip | None]:
    import meiva
    from meiva.sdk_surface.help import is_documentable_target

    head, _, rest = path.partition(".")
    try:
        target: object = getattr(meiva, head)
    except AttributeError:
        return None, ExpandSkip.UNRESOLVED
    for segment in rest.split(".") if rest else ():
        try:
            target = getattr(target, segment)
        except AttributeError:
            return None, ExpandSkip.MISSING_MEMBER
    if not is_documentable_target(target):
        return None, ExpandSkip.NOT_A_TARGET
    return target, None


# meiva:note
# The concrete expansion list one section renders: plugin declarations first, `*` replaced by the
# namespaces this process serves, then the configured entries, deduplicated, minus the ones
# MEIVA_SDK_DISABLE removes. It lives here rather than in `system_prompt.py` because the list is a
# read of the live SDK, and the context note that reports a dropped entry asks the same question —
# one owner answers both, so the note cannot name an entry the reference rendered.
def effective_sdk_expand(
    configured: Sequence[str], *, disabled: Sequence[str], plugin_expansions: Sequence[str]
) -> list[str]:
    plan = expand_plan(
        configured,
        plugin_expansions=plugin_expansions,
        discovered=discover_all_namespaces(disabled) if "*" in configured else (),
    )
    for path in plan.refused:
        logging.getLogger(__name__).warning(
            "sdk_expand_in_system_prompt: refusing %r — expanding one member of a "
            "capability surface would render that skill's whole body (or that "
            "server's tool schemas) into every prompt and record a bogus 'loaded' "
            "attribution. Preload a skill with skills_to_expand_at_start; leave a "
            "server's tools to meiva.help(meiva.mcps.<server>)",
            path,
        )
    return list(plan.paths)


# meiva:note
# The configured entries this window's SDK reference will not carry, asked of the same plan the
# reference itself is rendered from (`effective_sdk_expand`), so the answer cannot disagree with
# what rendered. It leaves out `UNRESOLVED` on purpose: an entry this process does not serve looks
# exactly like a plugin that is not enabled here, and a note that fired for every such plugin would
# be noise in every window. Silent either way: the renderer owns the warning, this answers the agent.
def skipped_expand_entries(
    configured: Sequence[str],
    *,
    disabled: Sequence[str],
    plugin_expansions: Sequence[str],
) -> tuple[SkippedExpand, ...]:
    wanted = effective_sdk_expand(
        configured, disabled=disabled, plugin_expansions=plugin_expansions
    )
    skipped: list[SkippedExpand] = []
    for path in wanted:
        if disabled_by_sdk_config(path, disabled):
            continue
        _target, kind = read_expand_entry(path)
        if kind is not None and kind is not ExpandSkip.UNRESOLVED:
            skipped.append(SkippedExpand(path, kind))
    return tuple(skipped)


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
        target, skipped = read_expand_entry(path)
        if skipped is ExpandSkip.UNRESOLVED:
            if report:
                logger.warning(
                    "sdk_expand_in_system_prompt: meiva.%s does not resolve and is not covered "
                    "by MEIVA_SDK_DISABLE (typo, or a plugin namespace whose plugin is not "
                    "loaded?), skipping",
                    path,
                )
            continue
        if skipped is ExpandSkip.MISSING_MEMBER:
            if report:
                logger.warning(
                    "sdk_expand_in_system_prompt: meiva.%s names no member of that namespace "
                    "(typo?), skipping",
                    path,
                )
            continue
        if skipped is ExpandSkip.NOT_A_TARGET:
            if report:
                logger.warning(
                    "sdk_expand_in_system_prompt: meiva.%s is not an SDK target (a module or "
                    "namespace, a routine, a class, or a documented constant), skipping",
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
