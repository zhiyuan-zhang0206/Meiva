"""Plugin-contributed skill-root providers — framework-internal registry.

A plugin declares (`PluginContributions.skill_sources`) a callable returning skill-root directories
computed at scan time (used for project-local skills whose location depends on runtime cwd).
`meiva.sdk_surface.install` collects them into the `Installation`; the scanner (`meiva.skills`) reads the
installed roots. A framework caller can also hold one extra provider for the duration of a call
(`scoped` — the ops runner's per-agent command view): that builds a new installation value with the
provider appended and takes it back afterwards.

The list lives off the agent-facing `meiva.skills` module, so the installer can write it and the scanner
can read it without either reaching through `meiva.skills` — which `MEIVA_SDK_DISABLE` may replace with a
stub (a hermetic bench that scopes the skills surface out). Per-process state: each agent is its
own process.

This module is framework-internal: not agent-facing, never in the
`meiva.help()` view.
"""

from __future__ import annotations

from collections.abc import Callable, Generator
from contextlib import contextmanager
from pathlib import Path

from . import meiva_module
from .installation import SLOT, SkillProvider, installed, slot
from .pure.plans.installation.slot_plan import mounted, unmounted


def add(provider: SkillProvider) -> Callable[[], None]:
    """Mount `provider` on this process's skill-root providers; returns the undo.

    The install collects a plugin's declared `skill_sources` into the installation it
    is building; this mounts one on the CURRENT value instead (tests and the `scoped`
    context manager). The undo removes exactly that provider."""
    prior = slot()
    mounted_value = mounted(prior, provider)
    setattr(meiva_module(), SLOT, mounted_value)

    def undo() -> None:
        write = unmounted(prior, mounted_value, slot(), provider)
        if write is not None:
            setattr(meiva_module(), SLOT, write.value)

    return undo


@contextmanager
def scoped(provider: SkillProvider) -> Generator[None]:
    """Hold `provider` for the duration of a `with` block and take back only that one.

    For a framework caller that needs a request-scoped skill root (the ops runner's per-agent
    command view): the installed plugin providers stay in place, and the scoped one never outlives
    the call even when it raises.
    """
    undo = add(provider)
    try:
        yield
    finally:
        undo()


def roots() -> list[Path]:
    """Flatten every registered provider's roots into one list (scan order)."""
    current = installed()
    if current is None:
        return []
    out: list[Path] = []
    for provider in current.skill_providers:
        out.extend(provider())
    return out
