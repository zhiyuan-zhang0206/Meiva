# meiva:note
# The wrap registration primitive — `meiva.extend.wrap` and its introspection.
#
# Agent visibility is governed by the `__all_for_meiva__` whitelist in
# `meiva/__init__.py`, not by a name's underscore prefix: this module stays out of
# the agent's `meiva.help()` view because it is absent from `__all_for_meiva__`. A
# plugin declares a wrap in its `contribute()`:
#
#     def audit_writes(inner, path, content):
#         log_audit("write", path, len(content))
#         return inner(path, content)
#
#     def contribute() -> PluginContributions:
#         return PluginContributions(sdk_wraps=(SdkWrap("files.write", audit_writes),))
#
# `meiva.sdk_surface.install` applies it through `apply_wrap(target, wrapper, plugin, layers)`, which
# installs `wrapper` around the callable at the dotted `meiva` path `target` (`"files.read"`,
# `"shell.run"`, `"agents.spawn"`, `"understand"`) and returns the `WrapAdded` record. `layers` is the
# install's build ledger (`{target: [WrapLayer, ...]}`, registration order); it is frozen
# into the `Installation` at the end of the build, and `stack` / `wrappers` read it back. The curated
# `meiva.extend` surface keeps the introspection (`stack`, `wrappers`). The wrapper's first parameter receives the current callable
# (`inner`) — the original, or the previous plugin's wrap when several layers
# stack — and the wrapper decides whether, when, and how many times to call it.
# `inner(*args, **kwargs) -> result`; the wrapper is Turing-complete Python and
# can express before / after / replace / retry with no schema ceremony. That is
# the point: a schema'd hook registry would cap extensions at anticipated shapes.
#
# **Why this exists instead of bare `setattr`.** A plugin monkey-patching
# `meiva.files.read = my_read` leaves no record of who changed it, in what order, or
# how to undo it — the old code compensated with a hand-rolled "I am the sole
# wrapper" assert on every target. The layer table here makes the wrap stack
# enumerable (`meiva.extend.stack(target)` / `meiva.extend.wrappers()`), deterministic
# (declaration order = plugin load order, and plugins load in sorted name order),
# and reversible (each `apply_wrap` returns a `WrapAdded` record, reversed newest-first by
# `meiva.sdk_surface.install.uninstall`), so the assert is gone and layering is allowed.
#
# **Lawfulness contract (enforced at review, not by types).** A wrapper is
# Turing-complete, so nothing mechanically stops it from misbehaving; three rules
# keep a stack composable and are checked in review:
#
# 1. Preserve the inner signature. Extend it by *adding* keyword arguments only;
#    never drop or reorder what callers pass to `inner`.
# 2. Never swallow an exception silently. Let it propagate, or re-raise with
#    context — a wrapper that eats errors turns a fail-fast core into a guessing
#    game.
# 3. Document any short-circuit (not calling `inner`) or multiple calls of
#    `inner`. The next author reading the stack must be able to reason about
#    control flow from the docstring.
#
# Rule 3's cases are also the ones worth measuring, so a plugin layer that calls
# `inner` anything other than exactly once emits one `plugin_activation` event
# (`base/packages/plugins/activation.py`) — the runtime half of the plugin attribution,
# and philosophy §6's obsolescence gauge for wrap-shaped shims. A layer that
# passes straight through records nothing: it always runs once installed, so
# counting it would measure the installation rather than the shim.
#
# The plugin *loader* (by-path import, fail-soft containment, the external
# `plugins/` directory scan) is a separate concern and lives in
# `base/packages/plugins/loading/plugin_loader.py`.
#

from __future__ import annotations

import functools
import inspect
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from types import ModuleType
from typing import Any

from . import meiva_module
from .installation import SLOT, Loading, WrapAdded, WrapLayer, installed, slot
from .pure.plans.installation.attempt_plan import attempt_cleared, attempt_marked
from .pure.plans.installation.wrap_plan import (
    activation_due,
    chained_face,
    is_public_dotted_path,
    layer_is_async,
    uncallable_message,
)
from .pure.plans.metering.recorder_plan import strip_recorders


def load_attempted() -> bool:
    return isinstance(slot(), Loading)


def mark_load_attempt() -> None:
    write = attempt_marked(slot())
    if write is not None:
        setattr(meiva_module(), SLOT, write.value)


def clear_load_attempt() -> None:
    write = attempt_cleared(slot())
    if write is not None:
        setattr(meiva_module(), SLOT, write.value)


class WrapError(Exception):
    """Root of `meiva.extend.wrap` failures. Plugin authors catch this for a coarse net."""


class WrapTargetError(WrapError):
    """`target` is not a dotted path of plain identifiers under `meiva`, or it does
    not resolve to a callable. Wrapping a missing / non-callable member is a
    plugin bug — fail fast at registration rather than at first agent call."""


def _record_activation(target: str, plugin: str, inner_calls: int) -> None:
    """Report one non-transparent firing of this layer — see `chained`. Lazy
    import keeps this leaf free of an `meiva -> shared` load-order dependency; the emit path
    itself swallows its own failures."""
    try:
        from base.packages.plugins.activation import record as record_activation
        from meiva.sdk_surface import settings as _settings

        model = _settings.agent_setting("llm_model")
    except Exception:
        from loguru import logger

        logger.opt(exception=True).warning(
            "wrap activation for {} by plugin {} not recorded: reading the model failed",
            target,
            plugin,
        )
        return
    record_activation(plugin, "sdkWraps", target, detail=f"inner_calls={inner_calls}", model=model)


# meiva:note
# Resolve a dotted path under `root` (the `meiva` module) to `(parent_object, attr_name)`.
#
# `"files.read"` -> `(meiva.files, "read")`; `"understand"` -> `(meiva, "understand")`. Missing
# declared attributes are WrapTargetError. Errors raised by a real attribute descriptor
# propagate unchanged.
def _locate(target: str, root: ModuleType) -> tuple[Any, str]:
    if not is_public_dotted_path(target):
        raise WrapTargetError(
            f"wrap target {target!r} must be a dotted path of identifiers under `meiva` "
            f"(no leading underscore), e.g. 'files.read' or 'agents.spawn'."
        )
    segments = target.split(".")
    obj: Any = root
    for seg in segments[:-1]:
        obj = _target_member(obj, seg, target)
    return obj, segments[-1]


def _target_member(parent: object, attr: str, target: str) -> object:
    """Read a declared target without mistaking a descriptor's error for absence."""
    missing = object()
    if inspect.getattr_static(parent, attr, missing) is missing:
        raise WrapTargetError(f"meiva.{target} does not resolve: {attr!r} is not declared.")
    return getattr(parent, attr)


# meiva:note
# Make `chained` present as the function it replaces (`chained_face` decides how).
def _install_metadata(
    chained: Callable[..., object],
    wrapper: Callable[..., object],
    current: Callable[..., object],
) -> None:
    face = chained_face(wrapper, current)
    chained.__name__ = face.name
    chained.__qualname__ = face.qualname
    chained.__module__ = face.module
    chained.__doc__ = face.doc
    for key, value in face.inherited.items():
        chained.__dict__.setdefault(key, value)
    if face.signature is not None:
        chained.__signature__ = face.signature  # type: ignore[attr-defined]


# meiva:note
# Install `wrapper` around the `meiva` callable at dotted `target`; returns the `WrapAdded`
# record, which names the parent and attribute it replaced so `install` can put the original back.
#
# `layers` is the caller's ledger (`{target: [WrapLayer, ...]}`, registration order):
# `meiva.sdk_surface.install` passes its build ledger, which the installation then carries.
# `target` is a path under `meiva` ("files.read", "shell.run", "agents.spawn", "understand").
# `wrapper(inner, *args, **kwargs)` is called in place of the target; `inner` is the current
# callable (the original, or the previous layer when plugins stack) and the wrapper decides
# whether / when / how often to call it. Layers compose in declaration order, with plugins
# installed in sorted name order, so later plugins wrap outermost; the order is inspectable via
# `stack` / `wrappers`.
#
# See the head of this module for the three-rule lawfulness contract (preserve the inner
# signature, never swallow exceptions, document short-circuits / multi-calls).
#
# Raises WrapTargetError when `target` is not a dotted identifier path under `meiva` or does not
# resolve to a callable.
def apply_wrap(
    target: str,
    wrapper: Callable[..., object],
    plugin: str,
    layers: dict[str, list[WrapLayer]],
    *,
    root: ModuleType,
) -> WrapAdded:
    parent, attr = _locate(target, root)
    current = _target_member(parent, attr, target)
    if not callable(current):
        raise WrapTargetError(uncallable_message(target, current))
    current = strip_recorders(current)

    @contextmanager
    def invocation() -> Generator[Callable[..., Any], None, None]:
        calls = [0]

        @functools.wraps(current)
        def counted(*inner_args: object, **inner_kwargs: object) -> object:
            calls[0] += 1
            return current(*inner_args, **inner_kwargs)

        try:
            yield counted
        finally:
            if activation_due(calls[0]):
                _record_activation(target, plugin, calls[0])

    def chained(*args: object, **kwargs: object) -> object:
        with invocation() as inner:
            return wrapper(inner, *args, **kwargs)

    if layer_is_async(current, wrapper):

        async def async_chained(*args: object, **kwargs: object) -> object:
            with invocation() as inner:
                awaitable: Any = wrapper(inner, *args, **kwargs)
                return await awaitable

        chained = async_chained

    _install_metadata(chained, wrapper, current)
    setattr(parent, attr, chained)

    layer = WrapLayer(target=target, plugin=plugin, wrapper=wrapper)
    layers.setdefault(target, []).append(layer)
    return WrapAdded(layer=layer, parent=parent, attr=attr, chained=chained, original=current)


def _installed_layers() -> Mapping[str, tuple[WrapLayer, ...]]:
    """The current installation's wrap layers (empty when nothing is installed)."""
    current = installed()
    return {} if current is None else current.wrap_layers


def stack(target: str) -> list[tuple[str, Callable[..., object]]]:
    """The wrap layers on `target`, innermost first (= registration / load order).

    Each entry is `(plugin, wrapper)`. Empty list when nothing wrapped `target` (or
    nothing is installed). Answers "who changed `meiva.<target>` on this machine" as one call.
    """
    return [(layer.plugin, layer.wrapper) for layer in _installed_layers().get(target, ())]


def wrappers() -> dict[str, list[tuple[str, Callable[..., object]]]]:
    """Every wrapped target -> its `stack(target)`. The whole-machine wrap map,
    the runtime answer to "what did plugins inject" that plugin-injection docs
    are generated from instead of hand-maintained."""
    return {
        target: [(layer.plugin, layer.wrapper) for layer in layers]
        for target, layers in _installed_layers().items()
    }
