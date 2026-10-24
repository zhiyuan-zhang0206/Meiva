# meiva:note
# The one writer of the `meiva` module: install a registry's SDK surface, undo it, nothing else.
#
# A plugin declares its SDK surface in `plugin.py`'s `contribute()`
# (`PluginContributions.sdk_namespaces`, `sdk_members`, `sdk_expansions`, `sdk_wraps`,
# `skill_sources`, `config`, `flags`) and never touches `meiva`. `install(registry)` applies
# every plugin's declaration, in registry order (plugin name order), through the primitives in
# `plugins.py` / `wraps.py` / `skill_sources.py` / `config_registration.py` / `flags.py`; each
# returns the record of what it did, which the installation keeps as its reversal ledger. Why
# this exists at all: agent code reaches the SDK as attribute access on the singleton `meiva`
# module, so the module itself is the one thing that cannot be passed around as a value — the
# write to it is concentrated here, once per process.
#
# `install` produces one **`Installation`** (defined in `installation.py`, which also owns the
# slot and the `installed()` read every lower SDK module uses): the admitted registry, the
# expansions, the wrap layers, the skill providers, resolved plugin configs, the metering
# ledger, sampling owner, SDK-disable entries, the faces flag, and the reversal records.
# Metadata is frozen; runtime owners retain their own synchronization. The holder is a single
# slot on the `meiva` module (`__plugin_installation__`); a change (an additive SDK-disable
# entry, a scoped skill root, the agent-runtime faces loading) builds a new value and swaps the
# holder. `uninstall` reverses the surface and empties the slot.
#
# Before any plugin applies, the env's `MEIVA_SDK_DISABLE` entries are applied (so a disabled
# plugin namespace is refused, exactly as it was when the env was applied at `import meiva`);
# later additions (per-agent config overlay, the eval-isolation boundary) apply additively on
# the installed value (`sdk_disable.apply_sdk_disable`). After the last plugin the SDK-usage
# recorder is installed over the final surface, so it sits outermost of any wrap layer (one
# count per agent call); `uninstall` removes it first for the same reason.
#
# Per plugin the order is: namespaces, members (a member may hang on the plugin's own
# namespace), expansions, wraps (a wrap target may be a namespace or member just added, or
# another plugin's), skill sources, flags, config. Explicit declaration refusals
# (`RegisterNamespaceError`, `WrapTargetError`, `PluginFlagError`, `SchemaDriftError`,
# `InvalidConfigData`) are rolled back whole, reported, and left out of the returned registry
# only if rollback succeeds. Other errors abort the entire installation and propagate unchanged
# after every reversal is attempted. Cleanup failures are fatal themselves when there is no
# primary error; otherwise they are notes on the primary error.
#
# `install` refuses to run twice: the one installation must be `uninstall`ed before the next,
# which is what a reload is (a new registry, a new install). Nothing here triggers a reload at
# runtime; the host loads once per process and a changed plugin set takes effect on the next
# host start.


from __future__ import annotations

import atexit
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import assert_never

from pydantic import BaseModel

from base.agents.sdk.call_policy import SamplingPolicyOwner
from base.api_contracts.caller_identity import CallerIdentity
from base.clock import Clock
from base.config.service_read import ConfigAuthority
from base.lm.catalog import ModelCatalog
from base.lm.catalog.provider_keys import ProviderKeys
from base.packages.plugin_config_contracts import PluginConfigImages
from base.packages.plugins import load_report
from base.packages.plugins.extensions import ExtensionRegistry, PluginContributions

from . import meiva_module, wraps
from . import plugins as _plugins
from .installation import (
    LOADING,
    SLOT,
    ConfigBound,
    EventPipelines,
    Installation,
    Loading,
    MemberAdded,
    NamespaceAdded,
    ProviderAdded,
    Reversal,
    SkillProvider,
    WrapAdded,
    WrapLayer,
    installed,
    slot,
)
from .installation import (
    expansions as expansions,
)
from .sdk_disable import mark_load_failed as mark_load_failed
from .wraps import (
    clear_load_attempt as clear_load_attempt,
)
from .wraps import (
    load_attempted as load_attempted,
)
from .wraps import (
    mark_load_attempt as mark_load_attempt,
)


def raise_load_failure() -> None:
    """Propagate a prior load failure instead of answering as if the surface were ready."""
    value = slot()
    if isinstance(value, Loading) and value.failure is not None:
        raise value.failure


def mark_faces_loaded() -> None:
    """Record that the registry's agent-runtime faces loaded: build a new value, swap the holder."""
    current = installed()
    if current is None or current.faces:
        return
    setattr(meiva_module(), SLOT, replace(current, faces=True))


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
# The installation of a process that mounts a skill root before any plugin load. It meters
# nothing, so the sampling owner it carries is never read; a later `install` replaces the whole
# value with its own root's owner.
def _bare_provider_mount(provider: SkillProvider) -> Installation:
    return replace(Installation.bare(sampling=SamplingPolicyOwner()), skill_providers=(provider,))


def mount_skill_provider(provider: SkillProvider) -> Callable[[], None]:
    prior = slot()
    mounted = (
        replace(prior, skill_providers=(*prior.skill_providers, provider))
        if isinstance(prior, Installation)
        else _bare_provider_mount(provider)
    )
    setattr(meiva_module(), SLOT, mounted)

    def undo() -> None:
        current = slot()
        if current is mounted:
            setattr(meiva_module(), SLOT, prior)
            return
        if isinstance(current, Installation):
            remaining = tuple(p for p in current.skill_providers if p is not provider)
            if not remaining and _is_bare_provider_mount(current):
                setattr(meiva_module(), SLOT, None)
            else:
                setattr(meiva_module(), SLOT, replace(current, skill_providers=remaining))

    return undo


# meiva:note
# Refuse a dotted SDK path that cannot be promoted into the expanded SDK reference: the path is
# empty or has an invalid / underscore segment.
def _check_expansion(path: str) -> None:
    segments = path.split(".")
    if not path or not all(s.isidentifier() and not s.startswith("_") for s in segments):
        raise _plugins.InvalidNamespaceNameError(
            f"sdk expansion path {path!r} is invalid — dotted identifiers "
            "without underscore prefixes (e.g. 'cwd', 'shell.sessions')."
        )


# meiva:note
# Apply one plugin's SDK declaration into `build`; each applied piece appends its reversal record.
# Returns the plugin's expansion paths. Raises on the first piece that cannot be applied.
def _apply(
    plugin: str,
    contributions: PluginContributions,
    build: Build,
    namespaces: dict[str, str],
    config_images: PluginConfigImages,
) -> list[str]:
    from base.packages.plugins import config_registration, flags

    promoted: list[str] = []
    for ns in contributions.sdk_namespaces:
        build.undo.append(_plugins.install_namespace(plugin, ns.name, ns.module, namespaces))
        namespaces[ns.name] = plugin
        if ns.expand:
            _check_expansion(ns.name)
            promoted.append(ns.name)
    for member in contributions.sdk_members:
        build.undo.append(_plugins.install_member(plugin, member.namespace, member.name, member.fn))
    for path in contributions.sdk_expansions:
        _check_expansion(path)
        promoted.append(path)
    for wrap in contributions.sdk_wraps:
        build.undo.append(
            wraps.apply_wrap(wrap.target, wrap.wrapper, plugin, build.layers, root=meiva_module())
        )
    for provider in contributions.skill_sources:
        build.providers.append(provider)
        build.undo.append(ProviderAdded(provider))
    for key in contributions.flags:
        flags.validate_flag_key(key)
    if contributions.config is not None:
        release = config_registration.bind_plugin_config(
            config_images.owner(plugin, contributions.config), build.configs
        )
        build.undo.append(ConfigBound(plugin, release))
    return promoted


# meiva:note
# Take a layer off a wrap ledger; an emptied target leaves the ledger.
def _drop_layer(layers: dict[str, list[WrapLayer]], layer: WrapLayer) -> None:
    target_layers = layers[layer.target]
    target_layers.remove(layer)
    if not target_layers:
        del layers[layer.target]


# meiva:note
# The mutable ledger `install` fills while it applies plugins; frozen into the Installation.
@dataclass(frozen=True)
class Build:
    namespaces: dict[str, str] = field(default_factory=dict[str, str])
    layers: dict[str, list[WrapLayer]] = field(default_factory=dict[str, list[WrapLayer]])
    providers: list[SkillProvider] = field(default_factory=list[SkillProvider])
    expansions: list[str] = field(default_factory=list[str])
    undo: list[Reversal] = field(default_factory=list[Reversal])
    configs: dict[str, BaseModel] = field(default_factory=dict[str, BaseModel])

    # meiva:note
    # The ledger of an installed surface, rebuilt from the frozen snapshots the installation kept,
    # so an installed surface is reversed through the same records and lists as an unfinished build.
    @classmethod
    def of(cls, installation: Installation) -> Build:
        return cls(
            layers={target: list(layers) for target, layers in installation.wrap_layers.items()},
            providers=list(installation.skill_providers),
            configs=dict(installation.configs),
        )

    # meiva:note
    # Reverse one ledger record and take it out of this build's lists (a wrap layer, a skill
    # provider). A wrap puts the original callable back only when its parent still holds the wrap's
    # own `chained` function, so a target replaced since is left alone. A bound config is released
    # by the function its binding returned.
    def revert(self, record: Reversal) -> None:
        match record:
            case NamespaceAdded() | MemberAdded():
                _plugins.uninstall(record)
            case WrapAdded():
                _drop_layer(self.layers, record.layer)
                if getattr(record.parent, record.attr, None) is record.chained:
                    setattr(record.parent, record.attr, record.original)
            case ProviderAdded():
                self.providers.remove(record.provider)
            case ConfigBound():
                record.release()
            case _:
                assert_never(record)


# meiva:note
# Attempt every reversal, newest first; preserve a primary failure or raise the first cleanup
# failure.
def _run(undo: list[Reversal], build: Build, primary: BaseException | None = None) -> None:
    failure = primary
    while undo:
        record = undo.pop()
        try:
            build.revert(record)
        except BaseException as exc:
            if failure is None:
                failure = exc
            else:
                failure.add_note(f"SDK rollback also failed: {type(exc).__name__}: {exc}")
    if primary is None and failure is not None:
        raise failure


def install(
    registry: ExtensionRegistry,
    report: load_report.Reporter | None = None,
    *,
    config_images: PluginConfigImages,
    catalog: ModelCatalog | None = None,
    provider_keys: ProviderKeys | None = None,
    authority: ConfigAuthority | None = None,
    sampling: SamplingPolicyOwner,
    clock: Clock | None = None,
    clients: EventPipelines | None = None,
    external_caller: CallerIdentity | None = None,
    disabled_entries: Sequence[str] = (),
) -> ExtensionRegistry:
    """Install `registry`'s SDK surface into `meiva`; return the registry of the plugins admitted.

    Raises:
        RuntimeError: a previous installation is still in place (`uninstall()` first).
        BaseException: an unexpected application or cleanup failure; already-applied
            pieces are undone before the original error propagates.
    """
    if installed() is not None:
        raise RuntimeError(
            "the SDK surface is already installed; uninstall() it before installing another registry"
        )
    from base.packages.plugins import flags, plugin_errors

    from . import metering, sdk_disable

    prior = slot()
    setattr(meiva_module(), SLOT, LOADING)
    build = Build()
    admitted: list[tuple[str, PluginContributions]] = []
    disabled = list(disabled_entries)
    try:
        if disabled:
            # meiva:note
            # Apply the supplied entries before any plugin: a disabled plugin namespace is
            # refused by install_namespace (same as when the env was applied at import).
            sdk_disable.apply_entries(disabled)
        for plugin, contributions in registry.plugins:
            mark = len(build.undo)
            claimed = dict(build.namespaces)
            try:
                paths = _apply(plugin, contributions, build, claimed, config_images)
            except (
                _plugins.RegisterNamespaceError,
                wraps.WrapTargetError,
                flags.PluginFlagError,
                plugin_errors.SchemaDriftError,
                plugin_errors.InvalidConfigData,
            ) as exc:
                partial = build.undo[mark:]
                del build.undo[mark:]
                _run(partial, build)
                load_report.reporter(report)(plugin, exc)
                continue
            build.namespaces.update(claimed)
            build.expansions.extend(paths)
            admitted.append((plugin, contributions))
        metered = metering.install(
            sampling, clients=clients, surface=metering.read_surface(meiva_module())
        )
    except BaseException as exc:
        _run(build.undo, build, exc)
        setattr(meiva_module(), SLOT, prior)
        raise
    installation = Installation(
        registry=ExtensionRegistry(tuple(admitted)),
        config_images=config_images,
        expansions=tuple(build.expansions),
        wrap_layers=MappingProxyType(
            {target: tuple(layers) for target, layers in build.layers.items()}
        ),
        skill_providers=tuple(build.providers),
        metered=metered,
        sampling=sampling,
        disabled=frozenset(disabled),
        faces=False,
        undo=tuple(build.undo),
        configs=MappingProxyType(dict(build.configs)),
        catalog=catalog,
        provider_keys=provider_keys,
        authority=authority,
        clock=clock,
        clients=clients,
        external_caller=external_caller,
    )
    setattr(meiva_module(), SLOT, installation)
    return installation.registry


def apply_config_overlay(overlay: dict[str, object]) -> None:
    """Apply this exec child's plugin overlay to its installed config image."""
    from base.packages.plugins import config_registration

    current = installed()
    if current is None:
        raise RuntimeError("the SDK surface must be installed before applying plugin config")
    configs = config_registration.plan_config_overlay(overlay, current.configs).plugin_configs
    setattr(meiva_module(), SLOT, replace(current, configs=MappingProxyType(configs)))


def uninstall() -> Installation | None:
    """Remove the SDK surface and return its owners for an explicit subsequent reinstall."""
    from . import metering

    installation = installed()
    # The recorder sits outermost over plugin wraps, so it comes off first.
    metering.uninstall(() if installation is None else installation.metered)
    if installation is None:
        # A `LOADING` marker is not an installation: leave it in place (a load may be
        # in flight; a failed attempt stays un-retried), a fresh install overwrites it.
        return None
    # meiva:link meiva/docs/sdk-surface/install-lifetime.meiva.okf.md
    atexit.unregister(installation.sampling.close)
    setattr(meiva_module(), SLOT, None)
    _run(list(installation.undo), Build.of(installation))
    return installation
