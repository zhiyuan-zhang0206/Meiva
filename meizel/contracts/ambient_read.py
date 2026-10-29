# meiva:note
# `ambient-read`: only a composition root reaches the world a process runs in.
#
# A file that takes the clock, the environment, the home or working directory, the platform, a
# process id, randomness, or the process state the repository keeps (its configuration, database,
# event bus, service endpoints) instead of being handed it is a decision that cannot be tested by
# value. The files that may do so are declared under `[roots]` in their directory's `meizel.toml`,
# each with the kinds it may acquire; every other acquisition in non-test code is a violation.
# Recorded like any other, a violation can be carried as debt except where the file is `strict`.

from __future__ import annotations

import ast
from collections.abc import Iterable, Mapping

from meizel.contracts.debt import file_key
from meizel.contracts.manifest import Manifest, effective_purity
from meizel.contracts.policy import POLICY, Policy
from meizel.graph.imports import Scope, collect_scope
from meizel.graph.model import (
    Acquisition,
    AmbientKind,
    Code,
    DebtKey,
    Diagnostic,
    MeizelError,
    Sources,
    Unless,
)
from meizel.graph.universe import Universe
from meizel.language.scan import FileScan, module_bindings
from meizel.lint_common import is_test_path

__all__ = [
    "ambient_diagnostics",
    "reader_problems",
    "sources_from",
    "stale_readers",
    "stale_roots",
]

_CALL_MARK = "()"
_READERS = "ambient-readers"
# meiva:note
# The roles whose source code is governed: what ships, the runnable templates and the tooling.
_GOVERNED_ROLES = ("application", "templates", "tooling", "frontend")
_WHAT = {
    AmbientKind.SETTINGS: "the process configuration",
    AmbientKind.DB: "a database handle built from the live settings",
    AmbientKind.ENDPOINTS: "the service endpoints built from the live settings",
    AmbientKind.BUS: "an event bus built from the live settings",
    AmbientKind.ENVIRON: "the environment",
    AmbientKind.CLOCK: "the clock",
    AmbientKind.HOME: "the home directory",
    AmbientKind.CWD: "the working directory",
    AmbientKind.PLATFORM: "the platform",
    AmbientKind.PID: "the process identity",
    AmbientKind.RANDOM: "randomness",
}
_HELD = frozenset({AmbientKind.SETTINGS, AmbientKind.DB, AmbientKind.ENDPOINTS, AmbientKind.BUS})
# meiva:note
# The three lines every ambient-read carries: the principle, the recipe and the rule.
_WHY = (
    "why: code that reads the world itself cannot be tested by handing it a value, and it answers "
    "differently on another machine or at another time; the world is read once, in the process's "
    "composition root, and passed down"
)
_INSTEAD_HELD = (
    "instead: take the handle or the config from the composition root as a required parameter or "
    "a field of a frozen config; only a root builds it"
)
_INSTEAD_STDLIB = (
    "instead: read it in the composition root and pass the value down; a decision in the middle "
    "becomes a pure function of its inputs"
)
_SEE = (
    "see: docs/conventions/python-conventions.md#composition-roots-and-required-dependencies; "
    "a file is declared a root under [roots] in its directory's meizel.toml, and only if it is one"
)


def _module_of(directory: str, file: str) -> str:
    stem = file.rsplit(".", 1)[0]
    parts = [*directory.split("/"), *([] if stem == "__init__" else [stem])]
    return ".".join(part for part in parts if part)


# meiva:note
# The state a repository keeps: modules that declare `[state]`, and the policy's names for it.
def sources_from(manifests: Mapping[str, Manifest], policy: Policy, universe: Universe) -> Sources:
    names: dict[str, AmbientKind] = {}
    calls: dict[str, AmbientKind] = {}
    for directory, manifest in manifests.items():
        for file, kinds in manifest.state.items():
            module = _module_of(directory, file)
            for kind in kinds:
                for name in policy.names("ambient", kind.value):
                    if name.endswith(_CALL_MARK):
                        calls[f"{module}.{name.removesuffix(_CALL_MARK)}"] = kind
                    else:
                        names[f"{module}.{name}"] = kind
    calls.update(_followed(universe, _readers(policy, call=True)))
    readers = _followed(universe, _readers(policy, call=False))
    names.update(readers)
    return Sources(names, calls, frozenset(readers), _spared(universe, policy))


# meiva:note
# Whether the rule governs this file: a governed top-level role (tests record no acquisitions).
def in_scope(path: str, manifests: Mapping[str, Manifest]) -> bool:
    root = manifests.get("")
    if root is None:
        return False
    top = path.split("/", 1)[0]
    return any(top in root.roles.get(role, ()) for role in _GOVERNED_ROLES)


def _diagnostic(scan: FileScan, acquisition: Acquisition, mode: str, owner: str) -> Diagnostic:
    kind, target = acquisition.kind, acquisition.target
    instead = _INSTEAD_HELD if kind in _HELD else _INSTEAD_STDLIB
    debt = (
        None
        if mode == "strict"
        else DebtKey(owner, file_key(owner, scan.path, f"{kind.value} {target}"))
    )
    return Diagnostic(
        scan.path,
        acquisition.line,
        Code.AMBIENT_READ,
        f"reads {_WHAT[kind]} (`{target}`) outside a composition root",
        (_WHY, instead, _SEE),
        debt,
    )


# meiva:note
# Every acquisition in a governed file whose directory manifest does not declare its kind.
def ambient_diagnostics(
    scans: Iterable[FileScan], manifests: Mapping[str, Manifest]
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for scan in scans:
        if not scan.ambient or not in_scope(scan.path, manifests):
            continue
        owner, _, name = scan.path.rpartition("/")
        manifest = manifests.get(owner)
        root = None if manifest is None else manifest.roots.get(name)
        allowed = frozenset(() if root is None else root.kinds)
        mode = effective_purity(scan.path, manifests)
        found.extend(
            _diagnostic(scan, acquisition, mode, owner)
            for acquisition in scan.ambient
            if acquisition.kind not in allowed
        )
    return found


# meiva:note
# A declared root kind that its file no longer acquires; only a whole run can know.
def stale_roots(
    scans: Mapping[str, FileScan], manifests: Mapping[str, Manifest]
) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for directory, manifest in sorted(manifests.items()):
        for name, root in manifest.roots.items():
            path = f"{directory}/{name}" if directory else name
            scan = scans.get(path)
            if scan is None:
                continue
            acquired = {acquisition.kind for acquisition in scan.ambient}
            found.extend(
                Diagnostic(
                    manifest.path,
                    manifest.line_of(name),
                    Code.STALE_ROOT,
                    f"[roots] {name!r} declares `{kind.value}` but the file no longer acquires it",
                    ("remove the kind from [roots]; a root declares what it reads, not more",),
                )
                for kind in root.kinds
                if kind not in acquired
            )
    return found


# meiva:note
# The policy's reader entries as `dotted name -> kind`: the calls (`name()`) or the bare names, which
# acquire where they are imported or read as an attribute.
def _readers(policy: Policy, *, call: bool) -> dict[str, AmbientKind]:
    return {
        name.removesuffix(_CALL_MARK): kind
        for kind in AmbientKind
        for name in policy.reasons(_READERS, kind.value)
        if name.endswith(_CALL_MARK) == call
    }


# meiva:note
# The `unless` clause of one entry: the parameter, and where it sits among the arguments that can
# be given by position. The reason the clause cannot be read - no such module, not a module-level
# function, no such parameter - comes back as text instead, for the stale-reader diagnostic.
def _unless(universe: Universe, target: str, parameter: str) -> Unless | str:
    located = _locate(universe, target)
    if located is None:
        return "names no tracked module"
    _, file, member = located
    if len(member) != 1:
        return f"is a method: `unless` is read for a module-level function only, in {file}"
    functions = [
        node
        for node in ast.parse(universe.load_text(file)).body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == member[0]
    ]
    if not functions:
        return f"is not a module-level function of {file}"
    arguments = functions[-1].args
    positional = [argument.arg for argument in (*arguments.posonlyargs, *arguments.args)]
    if parameter in positional:
        return Unless(parameter, positional.index(parameter))
    if any(argument.arg == parameter for argument in arguments.kwonlyargs):
        return Unless(parameter, None)
    return f"names no parameter {parameter!r}"


# meiva:note
# The readers whose read a passed parameter spares, by the name a call spells them: the clause is a
# property of the entry, so a re-exported spelling keeps it only where the policy lists that
# spelling. Entries whose clause cannot be read are left to `stale_readers`.
def _spared(universe: Universe, policy: Policy) -> dict[str, Unless]:
    found: dict[str, Unless] = {}
    for kind in AmbientKind:
        for name, parameter in policy.unless_for(_READERS, kind.value).items():
            target = name.removesuffix(_CALL_MARK)
            resolved = _unless(universe, target, parameter)
            if isinstance(resolved, Unless):
                found[target] = resolved
    return found


# meiva:note
# The shape of the policy's `[ambient-readers.<kind>]` entries: a fully qualified call
# (`pkg.module.name()`, `pkg.module.Class.method()`) or name (`pkg.module.NAME`), listed under
# exactly one kind.
def reader_problems(policy: Policy) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    seen: set[str] = set()
    for kind in AmbientKind:
        for name in policy.reasons(_READERS, kind.value):
            where = f"[{_READERS}.{kind.value}] {name!r}"
            line = policy.line_of(name)
            if "." not in name.removesuffix(_CALL_MARK):
                message = (
                    f"{where} must be qualified, as in `pkg.module.name()` or `pkg.module.NAME`"
                )
            elif name in seen:
                message = f"{where} is listed under more than one kind: a reader has one kind"
            else:
                seen.add(name)
                continue
            found.append(Diagnostic(POLICY, line, Code.POLICY_SYNTAX, message))
    for kind in AmbientKind:
        for name in policy.unless_for(_READERS, kind.value):
            if name.endswith(_CALL_MARK):
                continue
            message = (
                f"[{_READERS}.{kind.value}] {name!r} is not a call: `unless` spares the read a call "
                f"makes, so list it as `{name}()` or drop the clause"
            )
            found.append(Diagnostic(POLICY, policy.line_of(name), Code.POLICY_SYNTAX, message))
    return found


# meiva:note
# The module that defines a dotted reader: its dotted name, its file and the member path below it
# (`name` or `Class.method`). The longest prefix that names a tracked module wins.
def _locate(universe: Universe, target: str) -> tuple[str, str, tuple[str, ...]] | None:
    parts = target.split(".")
    for end in range(len(parts) - 1, 0, -1):
        dotted = ".".join(parts[:end])
        module = universe.resolve_module(dotted)
        if module is not None and module.file is not None:
            return dotted, module.file, tuple(parts[end:])
    return None


# meiva:note
# How a module defines a top-level name: "function", "class" or "value" (an assignment); None when
# it does so some other way. The last definition wins, as it does at run time.
def _definition(universe: Universe, file: str, name: str) -> str | None:
    found: str | None = None
    for node in ast.parse(universe.repo.load_text(file)).body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name == name:
            found = "function"
        elif isinstance(node, ast.ClassDef) and node.name == name:
            found = "class"
        elif isinstance(node, ast.Assign | ast.AnnAssign):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == name for target in targets):
                found = "value"
    return found


# meiva:note
# An entry is written the way its target is used: `name()` for a function, a class or a method (a
# call), `name` for a value. The other spelling would match nothing, and `stale-reader` is the one
# place that notices.
def _spelling_problem(
    universe: Universe, file: str, member: tuple[str, ...], target: str, *, call: bool
) -> str | None:
    if len(member) != 1:
        return None if call else f"`{'.'.join(member)}` is a method: list it as `{target}()`"
    (name,) = member
    kind = _definition(universe, file, name)
    if call and kind == "value":
        return f"`{name}` is a value, not a callable: list it as `{target}`, without `()`"
    if not call and kind == "function":
        return f"`{name}` is a function: list it as `{target}()`"
    return None


# meiva:note
# Why a reader can no longer be found, or None when the module that the policy names defines it:
# a `def`, a class or an assignment there, or a method of a class there, spelled as it is used. A
# name the module only imports is not a definition: a function that moved leaves a re-export
# behind, and a call imported from the new place would no longer be seen.
def _missing_reader(universe: Universe, target: str, *, call: bool) -> str | None:
    located = _locate(universe, target)
    if located is None:
        return "names no tracked module"
    module, file, member = located
    bindings = module_bindings(universe, file)
    head = member[0]
    if bindings.dynamic:
        return None
    if len(member) == 1 and head in bindings.defined:
        return _spelling_problem(universe, file, member, target, call=call)
    if len(member) == 1:
        if head in bindings.imported:
            return f"`{head}` is imported by {file}, not defined there; list the module that defines it"
        return f"`{head}` is not defined in {file}"
    cls = next((facts for facts in bindings.classes if facts.name == head), None)
    if cls is None:
        return f"names no module `{module}.{head}` and no class `{head}` in {file}"
    if len(member) == 2 and member[1] in cls.methods:
        return _spelling_problem(universe, file, member, target, call=call)
    return f"class `{head}` in {file} defines no `{'.'.join(member[1:])}`"


# meiva:note
# A listed reader that no longer exists fails, like any other stale allowance; only a
# whole-repository run knows.
def stale_readers(universe: Universe, policy: Policy) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    moved = ("remove the entry, or fix the name if the function moved",)
    clause = ("fix the parameter name, or drop the `unless` clause",)
    for kind in AmbientKind:
        spared = policy.unless_for(_READERS, kind.value)
        for name in policy.reasons(_READERS, kind.value):
            target = name.removesuffix(_CALL_MARK)
            problem = _missing_reader(universe, target, call=name.endswith(_CALL_MARK))
            hint = moved
            if problem is None and name in spared:
                resolved = _unless(universe, target, spared[name])
                problem = resolved if isinstance(resolved, str) else None
                hint = clause
            if problem is not None:
                found.append(
                    Diagnostic(
                        POLICY,
                        policy.line_of(name),
                        Code.STALE_READER,
                        f"[{_READERS}.{kind.value}] {name!r} {problem}",
                        hint,
                    )
                )
    return found


_REEXPORT_HOPS = 12


# meiva:note
# The names a module binds by importing, as `local name -> dotted origin`; an unparsable file binds none.
def _imports(universe: Universe, path: str) -> dict[str, str]:
    text = universe.repo.load_text(path)
    try:
        tree = ast.parse(text, filename=path)
    except (SyntaxError, ValueError):
        return {}
    scope = Scope(None)
    collect_scope(scope, tree.body, path.split("/")[:-1], walrus=":=" in text)
    return scope.imports


type _Spellings = dict[str, dict[str, AmbientKind]]


# meiva:note
# What can be re-exported, by its dotted origin: `module.name` -> {tail: kind}, where the tail is
# the method path below it (empty for a function).
def _origins(universe: Universe, readers: Mapping[str, AmbientKind]) -> _Spellings:
    exported: _Spellings = {}
    for target, kind in readers.items():
        located = _locate(universe, target)
        if located is not None:
            module, _, member = located
            exported.setdefault(f"{module}.{member[0]}", {})[".".join(member[1:])] = kind
    return exported


# meiva:note
# One round: every module among `candidates` that imports a known origin spells it under its own
# name too. Returns whether a new spelling appeared.
def _spread(
    universe: Universe,
    candidates: Iterable[str],
    exported: _Spellings,
    parsed: dict[str, dict[str, str]],
) -> bool:
    grew = False
    for path in sorted(candidates):
        module = _module_of(*path.rpartition("/")[::2])
        if path not in parsed:
            parsed[path] = _imports(universe, path)
        for local, origin in parsed[path].items():
            for tail, kind in list(exported.get(origin, {}).items()):
                spelled = exported.setdefault(f"{module}.{local}", {})
                if tail not in spelled:
                    spelled[tail] = kind
                    grew = True
    return grew


# meiva:note
# The readers and every spelling a module gives them by importing them at its top level: a call
# through a re-export is still the call of the reader. Only the non-test sources that mention one
# of the names are read, so the cost follows the readers, not the repository.
def _followed(universe: Universe, readers: Mapping[str, AmbientKind]) -> dict[str, AmbientKind]:
    exported = _origins(universe, readers)
    repo = universe.repo
    seen: set[str] = set()
    candidates: set[str] = set()
    parsed: dict[str, dict[str, str]] = {}
    for _ in range(_REEXPORT_HOPS + 1):
        tokens = {origin.rpartition(".")[2] for origin in exported} - seen
        seen |= tokens
        candidates.update(p for p in repo.grep_words(tokens, ".py") if not is_test_path(p))
        if not _spread(universe, candidates, exported, parsed):
            break
    else:
        raise MeizelError(
            f"the re-exports of the listed readers need more than {_REEXPORT_HOPS} rounds of "
            "the scan to follow (a chain of 12 modules is always followed); list the module "
            "that defines the reader instead"
        )
    return {
        f"{origin}.{tail}" if tail else origin: kind
        for origin, tails in exported.items()
        for tail, kind in tails.items()
    }
