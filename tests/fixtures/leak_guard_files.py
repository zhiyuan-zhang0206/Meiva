# meiva:note
# The leak guard's file boundary: which first-party module callables a file left pointing at another
# object. The comparison lives here so the guard's plugin module keeps one job (the fixture, the
# modes, the report); this module only holds state and compares it.
from __future__ import annotations

import operator
import sys
import time
import types
from collections.abc import Callable, Iterable, Sequence
from itertools import chain, compress, repeat
from typing import Any

_now = time.perf_counter_ns
_is_dunder = operator.methodcaller("startswith", "__")
_module_dict = operator.attrgetter("__dict__")
_dict_values: Callable[[dict[str, Any]], Iterable[Any]] = dict[str, Any].values
_dict_get: Callable[[dict[str, Any], str], Any] = dict[str, Any].get
MISSING = object()
# meiva:note
# (module name, attribute, the object it pointed at when the file began, the object found at its end)
Replaced = tuple[str, str, Any, Any]
# meiva:note
# Cached per-module snapshot: (module name, module dict, all keys, all values, watched callable keys, watched callables).
_Entry = tuple[
    str,
    dict[str, Any],
    tuple[str, ...],
    tuple[Any, ...],
    tuple[str, ...],
    tuple[Any, ...],
]
_Flat = tuple[
    tuple[str, ...],
    tuple[Any, ...],
    tuple[dict[str, Any], ...],
    tuple[str, ...],
    tuple[Any, ...],
]


# meiva:note
# `new` names the same thing `old` did, and the module that defines it serves that very object now:
# its body ran again in the dict the process already registered. That is the plugin loader's design
# (`base/packages/plugins/loading/plugin_loader.py` re-executes a spec in place, its #147 contract),
# not a test leaving process state behind — a name a face imports from a sibling face is rebound to
# the sibling's new class, instance and all. `#7832` (2026-10-30) named this after the guard reddened
# every shard that loaded the plugin faces twice: `agent_runtime.FleetConfig` is `default_config`'s
# class, re-executed, and the hook objects beside it are instances of a class whose module ran again.
# A name defined in the compared module itself stays exempt outright: that is that module's own body
# running again, which no comparison can tell from a redefinition.
#
# What it deliberately still reports: a stand-in whose definition is elsewhere (a class a test file
# defines) or under another name, and a binding the defining module no longer serves. A test that
# installs its own object therefore cannot borrow this exemption by naming it the same.
def _definition(value: object) -> tuple[str, str] | None:
    subject = value if isinstance(value, (type, types.FunctionType)) else type(value)
    module = getattr(subject, "__module__", None)
    name = getattr(subject, "__qualname__", None)
    if module is None or module == "builtins" or name is None or "." in name:
        return None
    return (module, name)


def _own_binding(old: object, new: object, compared: str) -> bool:
    if getattr(new, "__module__", None) == compared:
        return True
    identity = _definition(new)
    if identity is None or identity != _definition(old):
        return False
    module, name = identity
    served = getattr(sys.modules.get(module), name, MISSING)
    return served is (new if isinstance(new, (type, types.FunctionType)) else type(new))


# meiva:note
# The names this check holds to: the callables of the module. A function or a class a test hands
# to a module is a stand-in every later reader calls; a plain object (the bound agent identity is
# one) is per-test state with its own owner (`tests/fixtures/identity_restore.py` puts that slot
# back after every test), and comparing it here would name every file as a leaker.
def _index_module(name: str, module_dict: dict[str, Any]) -> _Entry:
    keys = tuple(module_dict)
    vals = tuple(module_dict.values())
    non_dunder = (
        map(operator.not_, map(_is_dunder, keys))
        if all(map(isinstance, keys, repeat(str)))
        else (isinstance(k, str) and not k.startswith("__") for k in keys)
    )
    mask = list(map(bool.__and__, non_dunder, map(callable, vals)))
    return (
        name,
        module_dict,
        keys,
        vals,
        tuple(compress(keys, mask)),
        tuple(compress(vals, mask)),
    )


def _flatten(entries: Sequence[_Entry]) -> _Flat:
    watched_attrs_per_mod = list(map(operator.itemgetter(4), entries))
    counts = map(len, watched_attrs_per_mod)
    return (
        tuple(chain.from_iterable(map(operator.itemgetter(2), entries))),
        tuple(chain.from_iterable(map(operator.itemgetter(3), entries))),
        tuple(chain.from_iterable(map(repeat, map(operator.itemgetter(1), entries), counts))),
        tuple(chain.from_iterable(watched_attrs_per_mod)),
        tuple(chain.from_iterable(map(operator.itemgetter(5), entries))),
    )


def _find_replaced(entries: Sequence[_Entry]) -> list[Replaced]:
    found: list[Replaced] = []
    for name, module_dict, _, _, watched_attrs, watched_vals in entries:
        module = sys.modules.get(name)
        if not isinstance(module, types.ModuleType) or vars(module) is not module_dict:
            continue
        if all(map(operator.is_, watched_vals, map(module_dict.get, watched_attrs))):
            continue
        for attr, old in zip(watched_attrs, watched_vals, strict=True):
            new = module_dict.get(attr, MISSING)
            if new is old or _own_binding(old, new, name):
                continue
            found.append((name, attr, old, new))
    return found


# meiva:note
# The existing callables a file left pointing at a different object than when it began.
#
# `ModuleWatch` compares dict sizes, so it cannot see an EXISTING name handed a new object: the size
# does not move. That shape is real (`#7796`: a fixture stand-in replaced `start_serving.is_serving`
# and the file never put the function back; every later test in the process read the stand-in, and
# the red one was elsewhere). Both `open` and `close` stay in C-level bulk passes (`map`, `compress`,
# `chain`, `operator.is_`) so coverage tracing pays no Python frame per module or attribute on clean
# file moves; only newly imported or mutated modules re-index their callables.
class FileIdentities:
    __slots__ = (
        "_all_keys",
        "_all_vals",
        "_dicts",
        "_entries",
        "_lens",
        "_names",
        "_watched_attrs",
        "_watched_dicts",
        "_watched_vals",
        "label",
        "ns",
        "replaced",
        "scans",
        "stopped",
    )

    def __init__(self) -> None:
        self.label: str | None = None
        self._names: list[str] = []
        self._dicts: list[dict[str, Any]] = []
        self._lens: list[int] = []
        self._entries: list[_Entry] = []
        self._all_keys: tuple[str, ...] = ()
        self._all_vals: tuple[Any, ...] = ()
        self._watched_dicts: tuple[dict[str, Any], ...] = ()
        self._watched_attrs: tuple[str, ...] = ()
        self._watched_vals: tuple[Any, ...] = ()
        self.replaced: list[Replaced] = []
        self.scans = 0
        self.ns = 0
        self.stopped = 0

    def _store_flat(self, flat: _Flat) -> None:
        (
            self._all_keys,
            self._all_vals,
            self._watched_dicts,
            self._watched_attrs,
            self._watched_vals,
        ) = flat

    def _extend_entries(self, added: list[_Entry]) -> None:
        if not added:
            return
        self._names.extend(map(operator.itemgetter(0), added))
        self._dicts.extend(map(operator.itemgetter(1), added))
        self._lens.extend(map(len, map(operator.itemgetter(2), added)))
        self._entries.extend(added)
        keys, vals, w_dicts, w_attrs, w_vals = _flatten(added)
        self._all_keys += keys
        self._all_vals += vals
        self._watched_dicts += w_dicts
        self._watched_attrs += w_attrs
        self._watched_vals += w_vals

    def _refresh_dirty_prefix(self, names: list[str], dicts: list[dict[str, Any]]) -> None:
        for i, entry in enumerate(self._entries):
            current = dicts[i]
            if (
                len(current) != self._lens[i]
                or not all(map(operator.is_, entry[3], current.values()))
                or tuple(current) != entry[2]
            ):
                refreshed = _index_module(names[i], current)
                self._entries[i] = refreshed
                self._lens[i] = len(refreshed[2])

    # meiva:note
    # Take the baseline from the modules the guard's watch already located. Unchanged modules reuse
    # their cached callable tuples through one bulk C identity pass; only newly imported or mutated
    # modules re-index.
    def open(self, modules: list[tuple[str, dict[str, Any]]], label: str) -> None:
        started = _now()
        self.label = label
        names = list(map(operator.itemgetter(0), modules))
        dicts = list(map(operator.itemgetter(1), modules))
        prev = len(self._entries)
        prefix_same = (
            prev <= len(modules)
            and names[:prev] == self._names
            and all(map(operator.is_, dicts[:prev], self._dicts))
        )
        if not prefix_same:
            self._names, self._dicts, self._lens, self._entries = [], [], [], []
            self._store_flat(((), (), (), (), ()))
            prev = 0
        prefix_dicts = dicts[:prev]
        prefix_clean = (
            list(map(len, prefix_dicts)) == self._lens
            and all(
                map(
                    operator.is_,
                    self._all_vals,
                    chain.from_iterable(map(_dict_values, prefix_dicts)),
                )
            )
            and all(map(operator.eq, self._all_keys, chain.from_iterable(prefix_dicts)))
        )
        if not prefix_clean:
            self._refresh_dirty_prefix(names, dicts)
            self._store_flat(_flatten(self._entries))
        if len(modules) > prev:
            self._extend_entries(list(map(_index_module, names[prev:], dicts[prev:])))
        self.ns += _now() - started

    # meiva:note
    # Compare against the file's baseline in one bulk C pass (`dict.get` + `operator.is_`). Only when
    # a module in `sys.modules` changed or a callable moved identity does `_find_replaced` inspect
    # individual modules and apply `_own_binding`.
    def close(self) -> list[Replaced]:
        if self.label is None:
            return []
        self.label = None
        started = _now()
        self.replaced = []
        live = list(map(sys.modules.get, self._names))
        same_dicts = all(map(isinstance, live, repeat(types.ModuleType))) and all(
            map(operator.is_, self._dicts, map(_module_dict, live))
        )
        if same_dicts and all(
            map(
                operator.is_,
                self._watched_vals,
                map(_dict_get, self._watched_dicts, self._watched_attrs),
            )
        ):
            self.scans += 1
            self.ns += _now() - started
            return []
        found = _find_replaced(self._entries)
        self.replaced = list(found)
        self.scans += 1
        self.ns += _now() - started
        return found

    # meiva:note
    # Close the file that ended (if any), put back what it left, and open `label`. The restore has to
    # happen BEFORE the next baseline: otherwise the new file's baseline would record the stand-in as
    # the original shape and report the restore itself as a replacement at its own end.
    def track(
        self, modules: list[tuple[str, dict[str, Any]]], label: str, *, restore: bool
    ) -> list[Replaced]:
        if label == self.label:
            return []
        found = self.close()
        if found and restore:
            self.restore()
            self.stopped += 1
        self.open(modules, label)
        return found

    # meiva:note
    # Put the replaced attributes back, so the next file's tests read the real objects.
    def restore(self) -> None:
        for name, attr, old, _ in self.replaced:
            module = sys.modules.get(name)
            if isinstance(module, types.ModuleType):
                vars(module)[attr] = old
        self.replaced = []
