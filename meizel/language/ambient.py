# meiva:note
# Where a file reaches the ambient world.
#
# An acquisition is the point at which code takes something from the process or the machine it
# runs in instead of being handed it: the clock, the environment, the home directory, the working
# directory, the platform, the process ids, randomness, and the process state a repository keeps
# itself (its configuration, its database, event bus and service endpoints). Only the point of
# acquisition is recorded. What the code does with the object afterwards is not followed: the
# analysis has no types, so `settings.x.y` after a `from ... import settings` is a plain attribute
# read of a name that was already acquired.
#
# The standard-library names are fixed here. The names of the state a repository keeps are the
# repository's own: a file declares under `[state]` in its `meizel.toml` that it holds state of a
# kind, and the policy lists, per kind, the names through which that state is reached.

from __future__ import annotations

import ast
from collections.abc import Iterator

from meizel.graph.model import Acquisition, AmbientKind, Sources
from meizel.graph.universe import Universe
from meizel.language.patching import PatchForms
from meizel.lint_common import is_test_path

__all__ = ["AmbientAccess", "AmbientRecorder", "stdlib_kind"]

# meiva:note
# The interval clocks. Reading one for a deadline, a duration or a TTL is the plain use of `time`
# that the conventions allow in code no test needs to steer; the read is a decision worth a root
# only where the same function also sleeps, because then how long to wait next is decided in
# a loop. The wall clock is always a window onto the world.
_INTERVAL_CLOCKS = frozenset(
    {"time.monotonic", "time.monotonic_ns", "time.perf_counter", "time.perf_counter_ns"}
)

_STDLIB: dict[str, AmbientKind] = {
    **dict.fromkeys(_INTERVAL_CLOCKS, AmbientKind.CLOCK),
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
        ("os.getpid", "os.getppid", "os.getuid", "os.geteuid", "os.getgid", "os.getegid"),
        AmbientKind.PID,
    ),
    **dict.fromkeys(("uuid.uuid1", "uuid.uuid4"), AmbientKind.RANDOM),
}
# meiva:note
# Modules every public name of which reaches the machine or draws randomness.
_WHOLE_MODULES = {
    "platform": AmbientKind.PLATFORM,
    "random": AmbientKind.RANDOM,
    "secrets": AmbientKind.RANDOM,
}
# meiva:note
# A generator built from an explicit seed is a value, not a window onto the process.
_SEEDED = frozenset({"random.Random"})


# meiva:note
# The kind and the acquired name of a dotted standard-library name, or None.
def stdlib_kind(dotted: str) -> tuple[AmbientKind, str] | None:
    parts = dotted.split(".")
    for end in range(1, len(parts) + 1):
        prefix = ".".join(parts[:end])
        if prefix in _STDLIB:
            return _STDLIB[prefix], prefix
    whole = _WHOLE_MODULES.get(parts[0])
    if whole is not None and len(parts) > 1 and ".".join(parts[:2]) not in _SEEDED:
        return whole, ".".join(parts[:2])
    return None


def _own_nodes(function: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterator[ast.AST]:
    pending: list[ast.AST] = list(function.body)
    while pending:
        node = pending.pop()
        yield node
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda):
            pending.extend(ast.iter_child_nodes(node))


def _sleeps(function: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    for node in _own_nodes(function):
        if isinstance(node, ast.Call):
            callee = node.func
            if (isinstance(callee, ast.Name) and callee.id == "sleep") or (
                isinstance(callee, ast.Attribute) and callee.attr == "sleep"
            ):
                return True
    return False


# meiva:note
# Collects the acquisitions of one file as the scanner visits it.
class AmbientRecorder:
    def __init__(self, sources: Sources) -> None:
        self.sources = sources
        self.items: list[Acquisition] = []
        self._waiting: list[bool] = []

    def enter(self, function: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._waiting.append(_sleeps(function))

    def leave(self) -> None:
        self._waiting.pop()

    def _add(self, line: int, kind: AmbientKind, target: str) -> None:
        self.items.append(Acquisition(line, kind, target))

    # meiva:note
    # A name imported with `from ... import`: state acquired where it is brought in.
    def imported(self, origin: str, line: int) -> None:
        kind = self.sources.names.get(origin)
        if kind is not None:
            self._add(line, kind, origin)

    # meiva:note
    # A name or attribute chain read in code. `bound` is the origin of its first name.
    def used(self, dotted: str, bound: str, line: int) -> None:
        found = stdlib_kind(dotted)
        if found is not None:
            kind, target = found
            if target not in _INTERVAL_CLOCKS or (self._waiting and self._waiting[-1]):
                self._add(line, kind, target)
            return
        parts = dotted.split(".")
        for end in range(len(bound.split(".")) + 1, len(parts) + 1):
            prefix = ".".join(parts[:end])
            kind = self.sources.names.get(prefix)
            if kind is not None:
                self._add(line, kind, prefix)
                return

    def called(self, callee: str | None, line: int) -> None:
        kind = None if callee is None else self.sources.calls.get(callee)
        if kind is not None and callee is not None:
            self._add(line, kind, callee)


# meiva:note
# A scanner that records where its file reaches the ambient world; test files record none.
class AmbientAccess(PatchForms):
    def __init__(
        self, universe: Universe, path: str, tree: ast.Module, *, walrus: bool, sources: Sources
    ) -> None:
        super().__init__(universe, path, tree, walrus=walrus)
        self.ambient = AmbientRecorder(sources)
        self.records_ambient = not (self.test_file or is_test_path(path))
