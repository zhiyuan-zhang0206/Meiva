# meiva:note
# The leak guard's file boundary: which first-party module callables a file left pointing at another
# object. The comparison lives here so the guard's plugin module keeps one job (the fixture, the
# modes, the report); this module only holds state and compares it.
from __future__ import annotations

import sys
import time
import types
from typing import Any

_now = time.perf_counter_ns
MISSING = object()
# meiva:note
# (module name, attribute, the object it pointed at when the file began, the object found at its end)
Replaced = tuple[str, str, Any, Any]


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
# The existing callables a file left pointing at a different object than when it began.
#
# `ModuleWatch` compares dict sizes, so it cannot see an EXISTING name handed a new object: the size
# does not move. That shape is real (`#7796`: a fixture stand-in replaced `start_serving.is_serving`
# and the file never put the function back; every later test in the process read the stand-in, and
# the red one was elsewhere). The comparison happens per FILE: one pass over the callables of the
# dicts the guard already located, where a per-test pass would cost a shard its own test time.
class FileIdentities:
    __slots__ = ("baseline", "label", "ns", "replaced", "scans", "stopped")

    def __init__(self) -> None:
        self.label: str | None = None
        # meiva:note
        # (module name, the module dict itself, {attr: the object it pointed at when the file began})
        self.baseline: tuple[tuple[str, dict[str, Any], dict[str, Any]], ...] = ()
        self.replaced: list[Replaced] = []
        self.scans = 0
        self.ns = 0
        self.stopped = 0

    # meiva:note
    # Take the baseline from the modules the guard's watch already located. The caller syncs the
    # watch first, so a module an import added since the last file is in this list.
    def open(self, modules: list[tuple[str, dict[str, Any]]], label: str) -> None:
        started = _now()
        self.label = label
        self.baseline = tuple(
            (name, module_dict, self._watched(module_dict)) for name, module_dict in modules
        )
        self.ns += _now() - started

    # meiva:note
    # The names this check holds to: the callables of the module. A function or a class a test hands
    # to a module is a stand-in every later reader calls; a plain object (the bound agent identity is
    # one) is per-test state with its own owner (`tests/fixtures/identity_restore.py` puts that slot
    # back after every test), and comparing it here would name every file as a leaker.
    @staticmethod
    def _watched(module_dict: dict[str, Any]) -> dict[str, Any]:
        return {k: v for k, v in module_dict.items() if not k.startswith("__") and callable(v)}

    # meiva:note
    # Compare against the file's baseline and forget it. `_own_binding` decides what a binding that
    # changed identity anyway still is: the same name served by the same module, which is a module's
    # body running again in the same dict (a plugin loader executing its spec a second time,
    # `importlib.reload`), not a test leaving a stand-in behind.
    def close(self) -> list[Replaced]:
        label, baseline = self.label, self.baseline
        self.label, self.baseline = None, ()
        if label is None:
            return []
        started = _now()
        found: list[Replaced] = []
        self.replaced = []
        for name, module_dict, before in baseline:
            module = sys.modules.get(name)
            if not isinstance(module, types.ModuleType):
                continue
            current = vars(module)
            if current is not module_dict:
                continue
            for attr, old in before.items():
                new = current.get(attr, MISSING)
                if new is old or _own_binding(old, new, name):
                    continue
                found.append((name, attr, old, new))
                self.replaced.append((name, attr, old, new))
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
