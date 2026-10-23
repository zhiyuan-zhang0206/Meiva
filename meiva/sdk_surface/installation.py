"""The installed SDK surface as a value, and the one slot on the `meiva` module that holds it.

`Installation` is what `meiva.sdk_surface.install` put into the process: the admitted registry, the
expansions, the wrap layers, the skill providers, resolved plugin configs, the metering ledger, the
sampling owner, SDK-disable entries and the faces flag. It is frozen: a change builds a new value
and swaps the holder. The holder is a single attribute on the `meiva` module (`__plugin_installation__`);
`installed()` reads it.

This module is the read side every SDK piece below the installer shares (`wraps`, `settings`,
`sdk_disable`, `skill_sources`, the agent kernel, tests). `install.py` is the one writer.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType, ModuleType
from typing import Any, Protocol, cast

from pydantic import BaseModel

from base.agents.sdk.call_policy import SamplingPolicyOwner
from base.clock import Clock
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.packages.plugin_config_contracts import PluginConfigImages, UnboundPluginConfigImages
from base.packages.plugins.extensions import EMPTY, ExtensionRegistry

from . import meiva_module


# meiva:note
# One installed wrap layer, in registration order. `target` is the dotted `meiva` path
# (`"files.read"`), `plugin` the plugin that declared it, `wrapper` the author's
# `wrapper(inner, *args, **kwargs)` function: plugin-declared registry data, the
# introspectable artifact (`wrapper.__module__` and its source say who and what).
@dataclass(frozen=True)
class WrapLayer:
    target: str
    plugin: str
    wrapper: Callable[..., Any]


SkillProvider = Callable[[], list[Path]]


# meiva:note
# What one applied piece of an SDK installation did to the process, as data: the installer's
# ledger of reversals. Uninstalling, or rolling back a plugin whose declaration failed halfway,
# reads the ledger newest-first and reverses each record (`install.revert`); the records name
# the objects they touched, so reversing one needs nothing from the code that applied it.
@dataclass(frozen=True)
class NamespaceAdded:
    name: str
    module: ModuleType
    on_surface: bool


@dataclass(frozen=True)
class MemberAdded:
    namespace: str
    name: str
    parent: ModuleType


@dataclass(frozen=True)
class WrapAdded:
    layer: WrapLayer
    parent: Any
    attr: str
    chained: Callable[..., Any]
    original: Callable[..., Any]


@dataclass(frozen=True)
class ProviderAdded:
    provider: SkillProvider


@dataclass(frozen=True)
class ConfigBound:
    plugin: str


type Reversal = NamespaceAdded | MemberAdded | WrapAdded | ProviderAdded | ConfigBound


# meiva:note
# What an installation needs of the client set it meters through: the lazily built event
# pipeline. A `base.agents.context.clients.ClientSet` satisfies it; naming only the capability
# keeps this module free of the client stack's imports.
class EventPipelines(Protocol):
    def event_pipeline(self) -> Any: ...


# meiva:note
# What the installed registry put into the process, and how to take it back.
#
# Frozen: a change builds a new value and swaps the holder (`SLOT`) — see the module docstring.
# `wrap_layers` and the provider tuple are snapshots taken when the installation was built;
# `undo` is the ledger of reversals in application order (run newest-first, once, on the way
# out).
@dataclass(frozen=True)
class Installation:
    registry: ExtensionRegistry
    config_images: PluginConfigImages
    expansions: tuple[str, ...]
    wrap_layers: Mapping[str, tuple[WrapLayer, ...]]
    skill_providers: tuple[SkillProvider, ...]
    metered: tuple[tuple[Any, str], ...]
    disabled: frozenset[str]
    faces: bool
    undo: tuple[Reversal, ...]
    sampling: SamplingPolicyOwner = field(default_factory=SamplingPolicyOwner)
    configs: Mapping[str, BaseModel] = field(default_factory=dict[str, BaseModel])
    catalog: ModelCatalog | None = None
    provider_keys: ProviderKeys | None = None
    authority: ConfigAuthority | None = None
    clock: Clock | None = None
    clients: EventPipelines | None = None

    # meiva:note
    # An installation that holds no plugin surface: only the root values it is given.
    @classmethod
    def bare(
        cls,
        *,
        catalog: ModelCatalog | None = None,
        provider_keys: ProviderKeys | None = None,
        authority: ConfigAuthority | None = None,
        clock: Clock | None = None,
        clients: EventPipelines | None = None,
        configs: Mapping[str, BaseModel] | None = None,
        skill_providers: tuple[SkillProvider, ...] = (),
    ) -> Installation:
        return cls(
            registry=EMPTY,
            config_images=UnboundPluginConfigImages(),
            expansions=(),
            wrap_layers=MappingProxyType({}),
            skill_providers=skill_providers,
            metered=(),
            disabled=frozenset(),
            faces=False,
            undo=(),
            configs=MappingProxyType({}) if configs is None else configs,
            catalog=catalog,
            provider_keys=provider_keys,
            authority=authority,
            clock=clock,
            clients=clients,
        )

    def require_catalog(self) -> ModelCatalog:
        """Return this installation's explicit model facts or refuse a non-model installation."""
        if self.catalog is None:
            raise RuntimeError("the SDK installation carries no ModelCatalog")
        return self.catalog

    # meiva:note
    # Return the provider keys this installation's root read or refuse a keyless one.
    def require_provider_keys(self) -> ProviderKeys:
        if self.provider_keys is None:
            raise RuntimeError("the SDK installation carries no ProviderKeys")
        return self.provider_keys


# The installation is recorded on the `meiva` module object itself — the one process-wide thing it
# describes — rather than in a module global of its own. A loading value also retains an
# existing surface during a faces upgrade; a failed value retains the original error.
SLOT = "__plugin_installation__"


@dataclass(frozen=True)
class Loading:
    """An in-flight load, or its failure; neither is a successful installation."""

    installation: Installation | None = None
    failure: BaseException | None = None


LOADING = Loading()


def slot() -> Installation | Loading | None:
    """The raw holder value: an `Installation`, a `Loading` marker, or None."""
    return cast(Installation | Loading | None, getattr(meiva_module(), SLOT, None))


def installed() -> Installation | None:
    """The current installation, or None before `install` / after `uninstall`."""
    value = slot()
    if isinstance(value, Loading):
        return value.installation
    return value if isinstance(value, Installation) else None


def provider_keys() -> ProviderKeys:
    installation = installed()
    if installation is None:
        raise RuntimeError("the SDK installation carries no ProviderKeys")
    return installation.require_provider_keys()


def model_catalog() -> ModelCatalog:
    installation = installed()
    if installation is None or installation.catalog is None:
        raise RuntimeError("the SDK installation carries no ModelCatalog")
    return installation.catalog


def config_authority() -> ConfigAuthority:
    installation = installed()
    if installation is None or installation.authority is None:
        raise RuntimeError("the SDK installation carries no ConfigAuthority")
    return installation.authority


def expansions() -> tuple[str, ...]:
    current = installed()
    return () if current is None else current.expansions
