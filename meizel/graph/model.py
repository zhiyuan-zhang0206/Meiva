# meiva:note
# Value types and the closed ambient vocabulary shared by every stage of the checker.

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "INTERVAL_CLOCKS",
    "LOOP_CLOCK",
    "OPEN_CALLS",
    "SEEDABLE_GENERATORS",
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
    "MeizelError",
    "Patch",
    "Problem",
    "PurityItem",
    "Restricted",
    "Sources",
    "TypeCastSite",
    "stdlib_kind",
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
    TYPE_CAST = "type-cast"
    ANY_COLLABORATOR = "any-collaborator"
    TYPE_CHECKING = "type-checking"
    AMBIENT_READ = "ambient-read"
    FAKE_ENTROPY = "fake-entropy"
    STALE_ROOT = "stale-root"
    STALE_READER = "stale-reader"
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


# meiva:note
# The interval clocks. Reading one for a deadline, a duration or a TTL is the plain use of `time`
# that the conventions allow in code no test needs to steer; the read is a decision worth a root
# only where the same function also waits, because then how long to wait next is decided in
# a loop. The wall clock is always a window onto the world.
LOOP_CLOCK = "asyncio.loop.time"
INTERVAL_CLOCKS = frozenset(
    {"time.monotonic", "time.monotonic_ns", "time.perf_counter", "time.perf_counter_ns", LOOP_CLOCK}
)

_STDLIB: dict[str, AmbientKind] = {
    **dict.fromkeys(INTERVAL_CLOCKS, AmbientKind.CLOCK),
    **dict.fromkeys(
        (f"os.{name}" for name in ("environ", "getenv", "putenv")), AmbientKind.ENVIRON
    ),
    **dict.fromkeys(
        (
            "time.time",
            "time.time_ns",
            "datetime.datetime.now",
            "datetime.datetime.utcnow",
            "datetime.datetime.today",
            "datetime.date.today",
        ),
        AmbientKind.CLOCK,
    ),
    **dict.fromkeys(("pathlib.Path.home", "os.path.expanduser"), AmbientKind.HOME),
    **dict.fromkeys(("os.getcwd", "os.getcwdb", "pathlib.Path.cwd"), AmbientKind.CWD),
    **dict.fromkeys(
        ("sys.platform", "os.name", "os.uname", "socket.gethostname", "socket.getfqdn"),
        AmbientKind.PLATFORM,
    ),
    **dict.fromkeys(
        (
            "os.getpid",
            "os.getppid",
            "os.getpgrp",
            "os.getuid",
            "os.geteuid",
            "os.getgid",
            "os.getegid",
            "threading.get_ident",
            "threading.get_native_id",
        ),
        AmbientKind.PID,
    ),
    **dict.fromkeys(("uuid.uuid1", "uuid.uuid4", "os.urandom"), AmbientKind.RANDOM),
}
# meiva:note
# Module-wide ambient vocabularies, narrowed by deterministic members below.
_WHOLE_MODULES = {
    "platform": AmbientKind.PLATFORM,
    "random": AmbientKind.RANDOM,
    "secrets": AmbientKind.RANDOM,
}
# meiva:note
# Calls that read the world only when they are given no argument beyond `arguments`:
# `time.localtime()` and `time.strftime(fmt)` the wall clock, `psutil.Process()` this process. With the
# argument the call describes a value it was handed and reads nothing. `Path()` is not listed: it is
# the empty path `.`, a value that reads nothing (and the sentinel of a walk up the parents).
OPEN_CALLS: dict[str, tuple[AmbientKind, int]] = {
    "psutil.Process": (AmbientKind.PID, 0),
    "time.gmtime": (AmbientKind.CLOCK, 0),
    "time.localtime": (AmbientKind.CLOCK, 0),
    "time.ctime": (AmbientKind.CLOCK, 0),
    "time.asctime": (AmbientKind.CLOCK, 0),
    "time.strftime": (AmbientKind.CLOCK, 1),
}
# meiva:note
# Constructors whose seed must be inspected at the call, rather than at a class reference.
SEEDABLE_GENERATORS = frozenset({"random.Random"})
# meiva:note
# Class references and constant-time comparison do not themselves acquire randomness.
_DETERMINISTIC = SEEDABLE_GENERATORS | frozenset({"secrets.compare_digest"})


# meiva:note
# The kind and the acquired name of a dotted standard-library name, or None.
def stdlib_kind(dotted: str) -> tuple[AmbientKind, str] | None:
    parts = dotted.split(".")
    for end in range(1, len(parts) + 1):
        prefix = ".".join(parts[:end])
        if prefix in _STDLIB:
            return _STDLIB[prefix], prefix
    whole = _WHOLE_MODULES.get(parts[0])
    if whole is not None and len(parts) > 1 and ".".join(parts[:2]) not in _DETERMINISTIC:
        return whole, ".".join(parts[:2])
    return None


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
