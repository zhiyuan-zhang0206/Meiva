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

from collections.abc import Iterable, Mapping

from meizel.contracts.debt import file_key
from meizel.contracts.manifest import Manifest, effective_purity
from meizel.contracts.policy import Policy
from meizel.graph.model import Acquisition, AmbientKind, Code, DebtKey, Diagnostic, Sources
from meizel.language.scan import FileScan

__all__ = ["ambient_diagnostics", "sources_from", "stale_roots"]

_CALL_MARK = "()"
# meiva:note
# The roles whose Python is governed: what ships, the runnable templates and the tooling.
_GOVERNED_ROLES = ("application", "templates", "tooling")
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
    stem = file.removesuffix(".py")
    parts = [*directory.split("/"), *([] if stem == "__init__" else [stem])]
    return ".".join(part for part in parts if part)


# meiva:note
# The state a repository keeps: modules that declare `[state]`, and the policy's names for it.
def sources_from(manifests: Mapping[str, Manifest], policy: Policy) -> Sources:
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
