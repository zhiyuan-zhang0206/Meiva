# meiva:note
# Plugin SDK extension primitives — namespace installation and its exception hierarchy.
#
# A plugin declares `sdk_namespaces` (`base.packages.plugins.extensions`); nothing here is a
# plugin-facing entry point. `meiva.sdk_surface.install` is the one writer of the `meiva` module and applies each
# declaration through `install_namespace`, which returns the undo that reverses it. The
# registries that used to live here (`_REGISTERED_NAMESPACES`, `REGISTERED_SDK_EXPANSIONS`) are gone: the installation records what it applied, and the prompt's expanded
# SDK reference reads `meiva.sdk_surface.install.expansions()`.

import sys as _sys
from collections.abc import Mapping
from types import ModuleType, SimpleNamespace

from base.host.boundary.json_values import is_object_list
from base.host.boundary.untyped import object_of

from . import CONTEXT_KEY, SdkProcessPurpose, meiva_module
from .installation import NamespaceAdded
from .sdk_disable import DisabledSDKModule


# meiva:note
# The live `__all_for_meiva__` list a namespace declares: the object this module appends plugin
# names to and removes them from, not a copy of the names it holds. A container that declares none
# cannot host a namespace.
def _surface_list(container: object) -> list[object] | None:
    declared = object_of(getattr, container, "__all_for_meiva__", None)
    return declared if is_object_list(declared) else None


def is_host_process() -> bool:
    return meiva_module().__dict__.get(CONTEXT_KEY) is SdkProcessPurpose.SHARED_HOST


def bind_host_process() -> None:
    slots = meiva_module().__dict__
    bound = slots.get(CONTEXT_KEY)
    if bound is not None and bound is not SdkProcessPurpose.SHARED_HOST:
        raise RuntimeError("cannot boot a shared host with an established SDK process context")
    slots[CONTEXT_KEY] = SdkProcessPurpose.SHARED_HOST


def bind_context(context: object) -> None:
    if is_host_process():
        raise RuntimeError("the shared agent host cannot bind an SDK process context")
    if not any(cls.__name__ == "MeivaContext" for cls in type(context).__mro__):
        raise TypeError("meiva.context must be an MeivaContext")
    meiva_module().__dict__[CONTEXT_KEY] = context


# ── Exception hierarchy (AGENTS.md SDK docstring rule: parent + subclass) ─
# Plugin authors use RegisterNamespaceError for coarse catch, specific
# subclass for fine catch.
# `meiva.__all_for_meiva__` is only modified by the install primitives here and their
# undo; external mutation would diverge it from what the install recorded.
# Convention: do not directly modify `meiva.__all_for_meiva__`.


# meiva:note
# Root of namespace install failures. Plugin authors use this for coarse catch.
class RegisterNamespaceError(Exception):
    pass


# meiva:note
# A namespace name or expansion path has an invalid or private identifier.
class InvalidNamespaceNameError(RegisterNamespaceError):
    pass


class InvalidNamespaceModuleError(RegisterNamespaceError):
    """module argument is not ModuleType or SimpleNamespace.

    Plugins mistakenly passing int / None / str / dict etc. cannot be
    correctly expanded by `help(meiva)` and don't support `meiva.{name}.foo`
    attribute access — raise immediately so the plugin author fixes it,
    rather than getting AttributeError six months later when the agent
    runs and losing the root cause.
    """


class NamespaceConflictError(RegisterNamespaceError):
    """name is already taken. Two subclasses distinguish framework-builtin vs another plugin."""


class FrameworkNamespaceConflictError(NamespaceConflictError):
    """name collides with a framework-builtin submodule (meiva.files / meiva.shell) or top-level attr —
    plugin must rename (framework-owned namespaces are not overridable)."""


class PluginNamespaceConflictError(NamespaceConflictError):
    """name already installed by another plugin — last-write-wins would
    cause plugins to silently trample on each other; fail-fast so plugin
    authors negotiate to rename. Message contains the name-holding plugin."""


def _materialize_namespace(name: str, ns: SimpleNamespace) -> ModuleType:
    """Turn a SimpleNamespace plugin namespace into a real module.

    `import meiva.<name>` resolves through `sys.modules`, whose entries are
    conventionally real modules: a bare SimpleNamespace there is missed by
    the module predicates (`ismodule()`) behind discovery and help rendering,
    so `dir()` / `getattr_static` probes would see an object that does not
    look like a module. Materializing it as a real module named `meiva.<name>`
    keeps every discovery path (`agent_visible_names`, help rendering)
    consistent — the members are copied into the module dict, and the
    `__all_for_meiva__` surface is synthesized from the public members when the
    namespace did not declare one.
    """
    module = ModuleType(f"meiva.{name}")
    module.__dict__.update(vars(ns))
    surface = getattr(ns, "__all_for_meiva__", None)
    if not isinstance(surface, list):
        surface = [member for member in vars(ns) if not member.startswith("_")]
    module.__all_for_meiva__ = surface  # type: ignore[attr-defined]
    return module


# meiva:note
# Add `meiva.<name>` for the agent to call (`meiva.<name>.foo()`); returns what was done.
#
# The namespace is also importable (`import meiva.<name>`) for the rest of the process.
# Boundary: in a cold process where the declaring plugin has not been installed yet (bare
# `import meiva`, no agent bootstrap), `import meiva.<name>` still raises ModuleNotFoundError —
# import resolution goes through sys.modules, not the package's dynamic attribute surface; only
# attribute access works then. That is expected, not a regression.
#
# Args: plugin: the declaring plugin, named in a conflict message. name: submodule name — valid
# Python identifier, no underscore prefix, cannot collide with meiva builtin modules / top-level
# attrs (case-sensitive check). module: ModuleType or SimpleNamespace — other types
# (int/None/str/dict) cannot be correctly expanded by `help(meiva)` and attribute access is
# inconsistent; raise immediately. taken: namespace name -> the plugin that installed it earlier
# in this install.
#
# Raises: InvalidNamespaceNameError: `name` is not a valid identifier or starts with underscore.
# InvalidNamespaceModuleError: `module` is not ModuleType / SimpleNamespace.
# PluginNamespaceConflictError: `name` already installed by another plugin.
# FrameworkNamespaceConflictError: `name` already taken by a framework-builtin submodule or top-
# level attr, or disabled by MEIVA_SDK_DISABLE.
# RegisterNamespaceError: the `meiva` package itself declares no `__all_for_meiva__` surface to
# register the namespace on — a broken install, not a plugin's mistake.
def install_namespace(
    plugin: str, name: str, module: object, taken: Mapping[str, str]
) -> NamespaceAdded:
    if not name.isidentifier():
        raise InvalidNamespaceNameError(
            f"namespace name {name!r} is invalid — must be a valid Python identifier."
        )
    if name.startswith("_"):
        raise InvalidNamespaceNameError(
            f"namespace name {name!r} cannot start with underscore — that's the framework / private namespace convention."
        )
    if not isinstance(module, (ModuleType, SimpleNamespace)):
        raise InvalidNamespaceModuleError(
            f"plugin {plugin!r} namespace {name!r}: module must be ModuleType or "
            f"SimpleNamespace, got {type(module).__name__} — agent uses "
            f"meiva.{name}.X requiring attr access, and help(meiva) must be able to introspect."
        )

    pkg = meiva_module()
    surface = _surface_list(pkg)
    if surface is None:
        raise RegisterNamespaceError(
            "the meiva package declares no __all_for_meiva__ surface to register a namespace on"
        )
    if name in taken:
        raise PluginNamespaceConflictError(
            f"meiva.{name} already installed by plugin {taken[name]!r} — same-name override not allowed; "
            f"plugin {plugin!r} must rename."
        )
    if hasattr(pkg, name):
        raise FrameworkNamespaceConflictError(
            f"meiva.{name} already exists (framework-builtin submodule or top-level attribute); "
            f"plugin {plugin!r} cannot override."
        )
    # MEIVA_SDK_DISABLE may have replaced the sys.modules entry with a disabled-module sentinel before this
    # plugin applies (install() applies the env entries before any plugin). The install must NOT clobber the sentinel — it is the
    # legible "disabled" error surface the disable machinery put there, and a disabled name is
    # framework-owned (not overridable), so fail loud with the same conflict class.
    if isinstance(_sys.modules.get(f"meiva.{name}"), DisabledSDKModule):
        raise FrameworkNamespaceConflictError(
            f"meiva.{name} is disabled by MEIVA_SDK_DISABLE — a disabled namespace is framework-owned and not overridable; plugin {plugin!r} must rename."
        )

    if isinstance(module, SimpleNamespace):
        module = _materialize_namespace(name, module)
    # Register under `meiva.<name>` in sys.modules so the namespace is a real importable submodule:
    # `import meiva.<name>` / `from meiva import <name>` resolve through sys.modules (LLM agents write
    # `import meiva.memory` out of Python habit). The same object is set on the package, so `meiva.<name>`
    # and `import meiva.<name>` always agree.
    _sys.modules[f"meiva.{name}"] = module
    setattr(pkg, name, module)
    # For `help()` rendering — module's real `__name__` is the plugin's internal path
    # (`plugins.X.Y`); `_qualname` is the agent-facing real name (`meiva.<name>`), which also lets
    # `_target_depth` compute heading depth correctly. Underscore prefix avoids being treated as an
    # exposable attr by child discovery.
    module._qualname = f"meiva.{name}"  # type: ignore[attr-defined]
    added_to_surface = name not in surface
    if added_to_surface:
        surface.append(name)

    return NamespaceAdded(name=name, module=module, on_surface=added_to_surface)


# meiva:note
# Reverse an added namespace: drop the package attribute and the surface entry, then the importable
# alias (otherwise `import meiva.<name>` keeps returning the stale module after the namespace is
# torn down) and the agent-facing name stamp (the module object outlives the namespace, a plugin
# keeps it, so a later install of the same object starts clean).
def uninstall(added: NamespaceAdded) -> None:
    pkg = meiva_module()
    _remove_attr(pkg, added.name)
    if added.on_surface:
        _remove_from_surface(object_of(getattr, pkg, "__all_for_meiva__", None), added.name)
    _sys.modules.pop(f"meiva.{added.name}", None)
    added.module.__dict__.pop("_qualname", None)


def _remove_attr(obj: object, name: str) -> None:
    """Delete `name` from `obj` when present."""
    if hasattr(obj, name):
        delattr(obj, name)


def _remove_from_surface(surface: object, name: str) -> None:
    """Remove `name` from an `__all_for_meiva__`-style surface list when it is
    one (the member host may be gone, in which case there is nothing to do)."""
    if is_object_list(surface) and name in surface:
        surface.remove(name)
