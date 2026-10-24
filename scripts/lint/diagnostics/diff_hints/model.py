# meiva:note
# The values the diff hints are computed from and decide with. Nothing here reads a
# file, a clock or the environment: the shell builds these values, the detectors map
# them to `Site`s and the renderer maps those to text.
from __future__ import annotations

import ast
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from meizel.graph.model import AmbientKind


# meiva:note
# The closed set of hint ids. A trailer names one of these; an unknown id in a
# `Hint-Ack` line is an error, not a silent no-op.
class HintId(StrEnum):
    AMBIENT_READ = "ambient-read"
    CALLABLE_DEFAULT = "callable-default"
    CALLABLE_BAG = "callable-bag"
    NONE_FALLBACK = "none-fallback"
    WILDCARD_CASE = "wildcard-case"
    PROTOCOL_OUTSIDE_BASE = "protocol-outside-base"
    ANY_COLLABORATOR = "any-collaborator"
    DELETED_TEST = "deleted-test"
    SERVICE_LOCATOR = "service-locator"
    MUTABLE_IN_FROZEN = "mutable-in-frozen"
    TEST_SEAM = "test-seam"
    TEST_SUBCLASS = "test-subclass"
    LAMBDA_DEPENDENCY = "lambda-dependency"
    SPY_ASSERT = "spy-assert"


FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


class FileKind(StrEnum):
    PRODUCTION = "production"
    TEST = "test"
    OTHER = "other"


# meiva:note
# One place a hint fired. `line` is the line in the new file (the old file for a
# deleted test), `detail` the one-line description printed after the id.
@dataclass(frozen=True)
class Site:
    hint: HintId
    path: str
    line: int
    detail: str


@dataclass(frozen=True)
class RemovedLine:
    old_line: int
    text: str


# meiva:note
# What one file's diff says: the new-file line numbers it added (and their text), and
# the old lines it removed. `deleted` marks a file that no longer exists after the change.
@dataclass(frozen=True)
class FileChange:
    path: str
    added: frozenset[int]
    added_text: tuple[str, ...]
    removed: tuple[RemovedLine, ...]
    deleted: bool


# meiva:note
# The top-level directories by role, read once from the root `meizel.toml` `[roles]`
# table by the shell. `production` is the application plus the templates (the code
# the red line governs), `tests` the top-level test directory role, `first_party`
# every directory the repository owns.
@dataclass(frozen=True)
class Roles:
    production: frozenset[str]
    tests: frozenset[str]
    first_party: frozenset[str]


# meiva:note
# One parsed Python file with its declared root kinds and the lines the change added.
# `names` maps each imported name to its dotted origin (`from time import monotonic` binds
# `monotonic` to `time.monotonic`). The index is built by one walk of the tree so that
# a detector looks only at what the change touched: `touched` holds the nodes that
# start on an added line; `functions`, `classes` and `calls` hold those whose span
# contains an added line.
@dataclass(frozen=True)
class Unit:
    path: str
    tree: ast.Module
    added: frozenset[int]
    kind: FileKind
    roles: Roles
    roots: frozenset[AmbientKind]
    names: Mapping[str, str]
    touched: tuple[ast.AST, ...]
    functions: tuple[FunctionNode, ...]
    classes: tuple[ast.ClassDef, ...]
    calls: tuple[ast.Call, ...]


# meiva:note
# One `Hint-Ack` trailer. `line` is None for a path-level answer.
@dataclass(frozen=True)
class Ack:
    hint: HintId
    path: str
    line: int | None
    reason: str
