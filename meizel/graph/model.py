"""Value types shared by every stage of the checker."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "Acquisition",
    "AmbientKind",
    "Code",
    "DebtKey",
    "Diagnostic",
    "EnumSubsetUse",
    "Fact",
    "FactKind",
    "MeizelError",
    "Patch",
    "Problem",
    "PurityItem",
    "Restricted",
    "Sources",
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
    FIRST_PARTY_PATCH = "first-party-patch"
    STDLIB_PATCH = "stdlib-patch"
    THIRD_PARTY_PATCH = "third-party-patch"
    ENV_PATCH = "env-patch"
    UNRESOLVED_PATCH = "unresolved-patch"
    MISSING_PATCH_TARGET = "missing-patch-target"
    PATCH_RAISING_FALSE = "patch-raising-false"
    TEST_SUBCLASS_OVERRIDE = "test-subclass-override"
    AMBIENT_READ = "ambient-read"
    STALE_ROOT = "stale-root"
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


# meiva:note
# What a process reads from the world it runs in, as a closed vocabulary.
#
# The first four are state a repository holds itself (declared under `[state]` and named in
# the policy); the rest are the standard library's windows onto the process and the machine.
class AmbientKind(StrEnum):
    SETTINGS = "settings"
    DB = "db"
    ENDPOINTS = "endpoints"
    BUS = "bus"
    ENVIRON = "environ"
    CLOCK = "clock"
    HOME = "home"
    CWD = "cwd"
    PLATFORM = "platform"
    PID = "pid"
    RANDOM = "random"


@dataclass(frozen=True, slots=True)
class Fact:
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


# meiva:note
# The point where a file reaches the ambient world: `target` is the dotted name it reads.
@dataclass(frozen=True, slots=True)
class Acquisition:
    line: int
    kind: AmbientKind
    target: str


# meiva:note
# The state a repository keeps itself: dotted name -> kind.
#
# `names` acquire the state when imported or read as an attribute of their module; `calls` only
# when called, so that importing a class for an annotation acquires nothing.
@dataclass(frozen=True, slots=True)
class Sources:
    names: dict[str, AmbientKind] = field(default_factory=dict[str, AmbientKind])
    calls: dict[str, AmbientKind] = field(default_factory=dict[str, AmbientKind])


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
