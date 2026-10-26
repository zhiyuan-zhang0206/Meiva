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
from meizel.graph.model import Acquisition, AmbientKind, Code, DebtKey, Diagnostic, Sources
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
    calls.update(_followed(universe, _readers(policy)))
    return Sources(names, calls)


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


def _policy_line(policy: Policy, name: str) -> int:
    quoted = f'"{name}"'
    return next((number for number, text in enumerate(policy.lines, 1) if quoted in text), 1)


# meiva:note
# The policy's reader entries as `dotted call -> kind`, in the order of the kinds.
def _readers(policy: Policy) -> dict[str, AmbientKind]:
    return {
        name.removesuffix(_CALL_MARK): kind
        for kind in AmbientKind
        for name in policy.reasons(_READERS, kind.value)
    }


# meiva:note
# The shape of the policy's `[ambient-readers.<kind>]` entries: a fully qualified function that
# is called (`pkg.module.name()` or `pkg.module.Class.method()`), listed under exactly one kind.
def reader_problems(policy: Policy) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    seen: set[str] = set()
    for kind in AmbientKind:
        for name in policy.reasons(_READERS, kind.value):
            where = f"[{_READERS}.{kind.value}] {name!r}"
            line = _policy_line(policy, name)
            if not name.endswith(_CALL_MARK) or "." not in name.removesuffix(_CALL_MARK):
                message = f"{where} must be a qualified call such as `pkg.module.name()`"
            elif name in seen:
                message = f"{where} is listed under more than one kind: a reader has one kind"
            else:
                seen.add(name)
                continue
            found.append(Diagnostic(POLICY, line, Code.POLICY_SYNTAX, message))
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
# Why a reader can no longer be found, or None when its module still defines it.
def _missing_reader(universe: Universe, target: str) -> str | None:
    located = _locate(universe, target)
    if located is None:
        return "names no tracked module"
    _, file, member = located
    bindings = module_bindings(universe.root, file)
    named = len(member) == 1 and member[0] in bindings.names
    method = len(member) == 2 and any(
        facts.name == member[0] and member[1] in facts.methods for facts in bindings.classes
    )
    if bindings.dynamic or named or method:
        return None
    return f"names no function or method `{'.'.join(member)}` in {file}"


# meiva:note
# A listed reader that no longer exists fails, like any other stale allowance; only a
# whole-repository run knows.
def stale_readers(universe: Universe, policy: Policy) -> list[Diagnostic]:
    found: list[Diagnostic] = []
    for kind in AmbientKind:
        for name in policy.reasons(_READERS, kind.value):
            problem = _missing_reader(universe, name.removesuffix(_CALL_MARK))
            if problem is not None:
                found.append(
                    Diagnostic(
                        POLICY,
                        _policy_line(policy, name),
                        Code.STALE_READER,
                        f"[{_READERS}.{kind.value}] {name!r} {problem}",
                        ("remove the entry, or fix the name if the function moved",),
                    )
                )
    return found


_REEXPORT_HOPS = 4


# meiva:note
# The names a module binds by importing, as `local name -> dotted origin`; an unparsable file binds none.
def _imports(universe: Universe, path: str) -> dict[str, str]:
    text = universe.load_text(path)
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
def _spread(universe: Universe, candidates: Iterable[str], exported: _Spellings) -> bool:
    grew = False
    for path in sorted(candidates):
        module = _module_of(*path.rpartition("/")[::2])
        for local, origin in _imports(universe, path).items():
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
    seen: set[str] = set()
    candidates: set[str] = set()
    for _ in range(_REEXPORT_HOPS):
        tokens = {origin.rpartition(".")[2] for origin in exported} - seen
        seen |= tokens
        candidates.update(p for p in universe.grep_words(tokens, ".py") if not is_test_path(p))
        if not _spread(universe, candidates, exported):
            break
    return {
        f"{origin}.{tail}" if tail else origin: kind
        for origin, tails in exported.items()
        for tail, kind in tails.items()
    }
