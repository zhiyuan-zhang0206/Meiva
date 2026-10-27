# meiva:note
# What the installation slot of the `meiva` module becomes, decided over the values the writer
# read from it: the value after the agent-runtime faces loaded, after a skill root is mounted or
# unmounted, and after a plugin config overlay. The writers (`skill_sources.py` for the skill
# root, `install.py` for the rest) read the slot, call these functions and store what they
# return; the transitions never touch the module.
# meiva:link meiva/docs/sdk-surface/install-lifetime.meiva.okf.md

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from types import MappingProxyType

from pydantic import BaseModel

from meiva.sdk_surface.installation import Installation, Loading, SkillProvider

type Slot = Installation | Loading | None


# meiva:note
# The installation once its agent-runtime faces are recorded as loaded; None when there is no
# installation or it already says so, so nothing is written.
def faces_loaded(current: Installation | None) -> Installation | None:
    if current is None or current.faces:
        return None
    return replace(current, faces=True)


# meiva:note
# Whether an installation holds nothing but mounted skill roots: no plugin, expansion, wrap,
# metered target, disabled entry, faces or reversal.
def _is_bare_provider_mount(installation: Installation) -> bool:
    return (
        not installation.registry.plugins
        and not installation.expansions
        and not installation.wrap_layers
        and not installation.metered
        and not installation.disabled
        and not installation.faces
        and not installation.undo
    )


# meiva:note
# The installation after `provider` is mounted: the prior installation with the provider
# appended, or the bare provider-mount installation when the slot holds none.
def mounted(prior: Slot, provider: SkillProvider) -> Installation:
    if isinstance(prior, Installation):
        return replace(prior, skill_providers=(*prior.skill_providers, provider))
    return Installation.bare_provider_mount(provider)


@dataclass(frozen=True, slots=True)
class SlotWrite:
    value: Slot


# meiva:note
# What unmounting `provider` writes, given the slot as it was before the mount (`prior`), the
# value the mount stored (`mounted_value`) and the value the slot holds now (`current`). Nothing
# changed since the mount: the prior value returns. An installation replaced since drops only this
# provider, and is cleared when that leaves it a bare provider mount with no provider. A slot that
# no longer holds an installation is left alone (None: no write).
def unmounted(
    prior: Slot, mounted_value: Installation, current: Slot, provider: SkillProvider
) -> SlotWrite | None:
    if current is mounted_value:
        return SlotWrite(prior)
    if isinstance(current, Installation):
        remaining = tuple(p for p in current.skill_providers if p is not provider)
        if not remaining and _is_bare_provider_mount(current):
            return SlotWrite(None)
        return SlotWrite(replace(current, skill_providers=remaining))
    return None


# meiva:note
# The installation with its plugin configs replaced by `configs` (a read-only view of them).
def overlaid(current: Installation, configs: Mapping[str, BaseModel]) -> Installation:
    return replace(current, configs=MappingProxyType(configs))
