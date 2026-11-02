# meiva:note
# Value types and the closed ambient vocabulary shared by every stage of the checker.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import NamedTuple

__all__ = [
    "OS_KINDS",
    "Acquisition",
    "AmbientKind",
    "AnyCollaboratorSite",
    "Code",
    "DebtKey",
    "Diagnostic",
    "EntropySite",
    "EnumSubsetUse",
    "Fact",
    "FactKind",
    "KindName",
    "MeizelError",
    "Patch",
    "Problem",
    "PurityItem",
    "Restricted",
    "Sources",
    "TypeCastSite",
    "Unless",
]


class MeizelError(Exception):
    """The repository cannot be analysed at all (git failure, unparsable source)."""


class FactKind(StrEnum):
    IMPORT = "import"
    ATTRIBUTE = "attribute"
    FIXTURE = "fixture"
    PLUGIN = "plugin"
    PATCH = "patch"
    LOAD = "load"


class Code(StrEnum):
    UNDECLARED_EDGE = "undeclared-edge"
    NOT_EXPORTED = "not-exported"
    PRIVATE_MODULE = "private-module"
    PRIVATE_NAME = "private-name"
    UNRESOLVED_IMPORT = "unresolved-import"
    RESTRICTED = "restricted-python"
    STALE_LOAD = "stale-load"
    STALE_EXTERNAL = "stale-external"
    MANIFEST_SYNTAX = "manifest-syntax"
    MANIFEST_KEY = "manifest-unknown-key"
    MANIFEST_CHILD = "manifest-unknown-child"
    MANIFEST_DUPLICATE = "manifest-duplicate"
    MANIFEST_SELF_EDGE = "manifest-self-edge"
    MANIFEST_CYCLE = "manifest-cycle"
    CYCLE_EDGE = "cycle-edge"
    MANIFEST_EXPORT_PRIVATE = "manifest-export-private"
    MANIFEST_STALE_EDGE = "manifest-stale-edge"
    MANIFEST_ROLES = "manifest-roles"
    MANIFEST_DEBT = "manifest-debt"
    POLICY_SYNTAX = "policy-syntax"
    UNLISTED_ALLOWANCE = "unlisted-allowance"
    DEBT_PAID = "debt-paid"
    BARE_DEBT = "bare-debt"
    FIRST_PARTY_PATCH = "first-party-patch"
    STDLIB_PATCH = "stdlib-patch"
    THIRD_PARTY_PATCH = "third-party-patch"
    ENV_PATCH = "env-patch"
    UNRESOLVED_PATCH = "unresolved-patch"
    MISSING_PATCH_TARGET = "missing-patch-target"
    PATCH_RAISING_FALSE = "patch-raising-false"
    TEST_SUBCLASS_OVERRIDE = "test-subclass-override"
    TYPE_CAST = "type-cast"
    ANY_COLLABORATOR = "any-collaborator"
    TYPE_CHECKING = "type-checking"
    AMBIENT_READ = "ambient-read"
    AMBIENT_UNJUDGED = "ambient-unjudged"
    LEGAL_READ_RECORDED = "legal-read-recorded"
    FAKE_ENTROPY = "fake-entropy"
    STALE_ROOT = "stale-root"
    STALE_READER = "stale-reader"
    STALE_APPROVAL = "stale-approval"
    APPROVED_BUDGET = "approved-budget"
    IMPURE_EDGE = "impure-edge"
    IMPURE_CALL = "impure-call"
    UNFROZEN_VALUE = "unfrozen-value"
    UNTYPED_EXPORT = "untyped-export"
    IMPURE_TEST_FIXTURE = "impure-test-fixture"
    STALE_EXPORT = "stale-export"
    PASSTHROUGH_FUNCTION = "pass-through-function"
    NESTED_ATTR_WRITE = "nested-attr-write"
    NESTED_PATCH_TARGET = "nested-patch-target"
    DOMAIN_SUBSET_LEAK = "domain-subset-leak"
    WIDE_FANOUT = "wide-fanout"
    WIDE_EXPORTS = "wide-exports"
    WIDE_PUBLIC_SURFACE = "wide-public-surface"
    WIDE_PARAMS = "wide-params"
    DEEP_ATTR_CHAIN = "deep-attr-chain"
    FAKE_TEST_ONLY_ISLAND = "fake-test-only-island"
    DISCONNECTED_ISLAND = "disconnected-island"


# meiva:note
# The name of a kind of ambient read as a manifest and the policy spell it: an `AmbientKind` value,
# or a state kind the policy declares under `[ambient]`.
type KindName = str


# meiva:note
# What a process reads from the world it runs in: the standard library's windows onto the process
# and the machine, as a closed vocabulary. The state a repository keeps itself (its configuration,
# a database handle, a bus) is not in it: each repository declares those kinds as the keys of the
# policy's `[ambient]` table, and a kind of either family travels as a `KindName`.
class AmbientKind(StrEnum):
    ENVIRON = "environ"
    CLOCK = "clock"
    HOME = "home"
    CWD = "cwd"
    PLATFORM = "platform"
    PID = "pid"
    RANDOM = "random"
    ARGV = "argv"


# meiva:note
# The names of `AmbientKind`: the kinds no repository declares, which a state kind may not reuse.
OS_KINDS: tuple[KindName, ...] = tuple(kind.value for kind in AmbientKind)


class Fact(NamedTuple):
    """File `src` uses `dst`: a file, or a directory for a declared dynamic load."""

    src: str
    dst: str
    line: int
    kind: FactKind
    names: tuple[str, ...] = ()
    detail: str = ""

    def edge(self) -> str:
        return f"{self.src} -> {self.dst}"


@dataclass(frozen=True, slots=True)
class Restricted:
    """A form outside the restricted Python subset, found in one file."""

    line: int
    form: str
    detail: str = ""


@dataclass(frozen=True, slots=True)
class Patch:
    """A patch: `form` applied to `target` (a dotted name, or the source text when it is unresolved)."""

    line: int
    form: str
    target: str
    code: Code
    imported: bool = False


@dataclass(frozen=True, slots=True)
class TypeCastSite:
    line: int
    column: int
    qualname: str


@dataclass(frozen=True, slots=True)
class AnyCollaboratorSite:
    line: int
    column: int
    qualname: str
    slot: str
    role: str


# meiva:note
# The point where a file reaches the ambient world: `target` is the dotted name it reads.
@dataclass(frozen=True, slots=True)
class Acquisition:
    line: int
    kind: KindName
    target: str


# meiva:note
# A reader that reads the world only when it is not handed one: the call is an acquisition unless
# it passes a value for `parameter`, which `position` places among the arguments that can be given
# by position (None when it is keyword-only).
@dataclass(frozen=True, slots=True)
class Unless:
    parameter: str
    position: int | None


# meiva:note
# The state a repository keeps itself: dotted name -> kind.
#
# `names` acquire the state when imported or read as an attribute of their module; `calls` only
# when called, so that importing a class for an annotation acquires nothing. The `uses` among the
# names (the policy's readers) acquire at each use of the imported name instead of at the import.
@dataclass(frozen=True, slots=True)
class Sources:
    names: dict[str, KindName] = field(default_factory=dict[str, KindName])
    calls: dict[str, KindName] = field(default_factory=dict[str, KindName])
    uses: frozenset[str] = frozenset()
    unless: dict[str, Unless] = field(default_factory=dict[str, Unless])


# meiva:note
# Where a file spends a reading that is not entropy as an identifier or a random draw: `what` is
# the reading and where it went (`time.monotonic_ns in an f-string`), or the wrapper over a clock
# (`interval-wrapper _monotonic`).
@dataclass(frozen=True, slots=True)
class EntropySite:
    line: int
    what: str


@dataclass(frozen=True, slots=True)
class PurityItem:
    line: int
    code: Code
    key: str
    message: str
    hint: str


@dataclass(frozen=True, slots=True)
class EnumSubsetUse:
    line: int
    cls_name: str
    members: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Problem:
    """A source-level defect found while extracting facts."""

    line: int
    code: Code
    message: str


@dataclass(frozen=True, slots=True)
class DebtKey:
    """Where a violation can be recorded: its owner directory and a line-free key in its baseline."""

    owner: str
    key: str


@dataclass(frozen=True, slots=True)
class Diagnostic:
    path: str
    line: int
    code: Code
    message: str
    hints: tuple[str, ...] = ()
    debt: DebtKey | None = None

    def render(self) -> str:
        head = f"{self.path}:{self.line}: [{self.code}] {self.message}"
        return "\n".join([head, *(f"    {hint}" for hint in self.hints)])
