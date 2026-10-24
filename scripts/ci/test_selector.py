"""Conservatively select backend tests for PR CI (enforce or shadow mode)."""

from __future__ import annotations

import argparse
import ast
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import cast

_SCRIPT_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(_SCRIPT_REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_REPOSITORY_ROOT))

from base.deploy.git.repo_change import (  # noqa: E402 - direct script entry needs repo root first
    is_doc_path,
)
from meizel.roles import all_packages, pytest_test_hosts  # noqa: E402 - standalone script
from scripts.ci.test_impact import (  # noqa: E402 - standalone script
    base_checkout,
    blocking_gaps,
    build_impact,
    code_names,
    module_files,
    plugin_modules,
    pyproject_change_is_local,
    python_files,
    unbounded_tests,
    unknown_diagnostics,
)
from scripts.structure.placement import ModuleIndex  # noqa: E402 - standalone script

# Global paths apply to every test, so a change keeps the full suite. This is the
# one owner of the concept; the root conftest's `pytest_plugins` modules join it
# per checkout (`Checkout.global_files`).
_GLOBAL_FILES = frozenset(
    {
        "pyproject.toml",  # interpreter, dependencies, pytest configuration, the test hosts
        "uv.lock",
        "conftest.py",  # root bootstrap: `pytest_plugins` loads for every test
        ".python-version",
        ".env.example",  # the documented settings surface
    }
)
_GLOBAL_PREFIXES = (
    "tests/fixtures/",  # the suite's global fixture plugins
    "db/",
    "migrations/",
    "deploy/",  # operational configuration that tests read by path, not by import
    "commands/",  # slash-command prompt data read at runtime and by tests by path
)
# meiva:note
# Prose under a global prefix is no global input: only its recognized readers can observe it.
# meiva:link docs/conventions/engineering/test-selection.md
_PROSE_SUFFIXES = (".md",)

# The root meizel manifests are declarations: `[roles]` names the top-level packages the
# selector classifies by, `[uses]`/`[debt.*]` and the policy feed the meizel checks (the
# structure job runs them over the whole tree on every CI run). No runtime behavior reads
# them, so a change selects the tests that read them (`_manifest_tests`) instead of the full
# suite. Test hosts come from pyproject.toml, which stays global.
_MANIFEST_FILES = frozenset({"meizel.toml", "meizel.policy.toml"})
# Repository-level configuration and tree-wide inputs: only the tree-scan tests
# can observe them.
_TREE_SCAN_ONLY_FILES = frozenset(
    {
        ".pre-commit-config.yaml",
        ".gitignore",
        ".gitattributes",
        ".gitleaks.toml",
        "LICENSE",
        "NOTICE",
    }
)
_TREE_SCAN_ONLY_PREFIXES = (
    ".github/",
    ".agents/",
    ".meiva/",
    ".trunk/",
    "demos/",
    "tests/e2e/",  # CI runs the whole e2e package for every diff that is not docs-only
    "typings/",
)
# Hidden tool directories that tests read by path rather than import. A change
# there also selects every collectable test whose source text names the directory
# (`referencing_tests`), so the set of readers is derived, never listed by hand.
_REFERENCED_ROOTS = (".github", ".agents", ".meiva", ".trunk")
# Top-level directories whose files belong to a package with an owning `tests/`.
_PACKAGE_ROOTS = frozenset(all_packages())
_QUEUE_PREFIXES = ("trunk-merge/", "trunk-temp/")
_NON_DOCUMENTATION_PREFIXES = ("schedules/", "tests/")
_TEST_FILE_PATTERN = re.compile(r"(?:test_.*|.*_test)\.py$")

# Tree-scan tests: their subject is the checked-out repository (the lint
# family and the repo-level CI/governance checks), so a changed source file
# can never reach them through the direct-import reverse map. They are pinned
# into every SELECTED candidate set — a green subset must not miss a tree-wide
# gate (task #4183: PR #3020's subset passed while the full population was red
# on tests/contracts/test_lint_event_kinds.py).
_TREE_SCAN_FILE_PATTERN = re.compile(r"test_lint_.*\.py$")
TREE_SCAN_TESTS = frozenset(
    {
        "scripts/ci/pull_requests/tests/test_ci_job_rerun.py",
        "tests/harness/test_ci_rerun_workflow.py",
        "scripts/ci/pull_requests/tests/test_ci_monitor.py",
        "tests/contracts/test_db_check_enum_sync.py",
        "tests/harness/test_pool_keepalives.py",
        "tests/harness/test_env_guard_canary.py",
        "scripts/ci/tests/test_harness_imports.py",
    }
)


class PathClass(StrEnum):
    """How one changed path contributes to the selection."""

    DOCUMENTATION = "documentation"  # runtime readers can require backend tests
    TEST = "test"  # a collectable test file and its runtime test consumers
    CONFTEST = "conftest"  # every collectable test below its directory
    PACKAGE = "package"  # runtime consumers plus the owning package's tests
    TREE_SCAN_ONLY = "tree-scan-only"  # repository-level input: tree-scan tests only
    MANIFEST = "manifest"  # root meizel manifest: its readers' tests and meizel's own
    FRONTEND = "frontend"  # ui/ non-Python: the frontend job owns it
    GLOBAL = "global"  # applies to every test: full suite
    DELETED = "deleted"  # absent from head: use base facts or explicitly keep FULL
    UNMAPPED = "unmapped"  # no rule owns it: full suite (the tracked tree has none)


# meiva:note
# Declarations and baselines that fixed repository paths or the fixture-scope reader consume, and
# the classes that name their own readers: a change to one runs its recognized readers only.
# meiva:link docs/conventions/engineering/test-selection.md
_DECLARATION_NAMES = frozenset({*_MANIFEST_FILES, "path_scopes.toml", "ambient_roots.toml"})
_BASELINE_PREFIXES = ("scripts/lint/baseline/", "scripts/structure/baseline/")
_BASELINE_FILES = frozenset({"scripts/structure/skip_sites.json"})
_MODELED_CLASSES = frozenset(
    {PathClass.TREE_SCAN_ONLY, PathClass.FRONTEND, PathClass.MANIFEST, PathClass.DOCUMENTATION}
)


@dataclass(frozen=True)
class SelectionResult:
    """One deterministic selector decision and the data behind it."""

    decision: str
    reason: str
    tests: tuple[str, ...] = ()
    est_seconds: float = 0.0
    full_est_seconds: float = 0.0
    blind_changed: tuple[str, ...] = ()
    forced_roots: tuple[str, ...] = ()
    map_source_count: int = 0
    diagnostics: tuple[str, ...] = ()
    shards: int = 0

    @property
    def count(self) -> int:
        """Return the selected test-file count."""
        return len(self.tests)

    def as_json(self) -> dict[str, object]:
        """Return the stable machine-readable selector payload."""
        return {
            "decision": self.decision,
            "reason": self.reason,
            "mode": selection_mode(),
            "tests": list(self.tests),
            "count": self.count,
            "est_seconds": self.est_seconds,
            "full_est_seconds": self.full_est_seconds,
            "blind_changed": list(self.blind_changed),
            "forced_roots": list(self.forced_roots),
            "map_source_count": self.map_source_count,
            "diagnostics": list(self.diagnostics),
            "shards": self.shards,
        }


@dataclass(frozen=True)
class Checkout:
    """The facts about one checked-out tree that classification needs."""

    repo_root: Path
    hosts: tuple[str, ...]
    collectable: frozenset[str]
    test_dirs: frozenset[str]  # `tests` directories that contain a collectable test
    global_files: frozenset[str]  # root-conftest plugin modules and their package inits


# meiva:note
# The width of the full backend fan-out in ci.yml: its matrix, `--count` and job names.
FULL_FANOUT_SHARDS = 8


def selection_mode() -> str:
    """The mode CI is running: the ci.yml TEST_SELECTION_MODE value.

    The selector itself behaves identically in either mode — the mode only
    decides how CI routes its decision — so this is audit metadata for the
    recorded payload, defaulting to the repository's enforce default.
    """
    return os.environ.get("TEST_SELECTION_MODE", "enforce")


def _is_test_dir_path(path: str, hosts: tuple[str, ...]) -> bool:
    """Whether a repo-relative path sits inside a test directory (top-level or a package's)."""
    parts = path.split("/")
    return parts[0] in hosts and "tests" in parts[:-1]


def _test_py_files(repo_root: Path) -> list[Path]:
    """Every .py file inside a test directory, in a stable order."""
    files: list[Path] = []
    hosts = pytest_test_hosts((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    for host in hosts:
        host_root = repo_root / host
        if host_root.is_dir():
            files.extend(
                path
                for path in host_root.rglob("*.py")
                if _is_test_dir_path(path.relative_to(repo_root).as_posix(), hosts)
            )
    return sorted(files)


def collectable_test_paths(repo_root: Path) -> set[str]:
    """Return the current non-e2e backend test-file universe: every test file in a tests/ directory."""
    return {
        path.relative_to(repo_root).as_posix()
        for path in _test_py_files(repo_root)
        if _is_collectable_test_path(path.relative_to(repo_root).as_posix())
    }


def tree_scan_tests(repo_root: Path) -> set[str]:
    """Repository-wide scan tests that every SELECTED subset must include.

    Membership is the ``test_lint_*.py`` family (by name, so a new lint test
    joins automatically) plus the explicit repo-level CI/governance checks in
    ``TREE_SCAN_TESTS``; both resolve against the collectable universe.
    """
    collectable = collectable_test_paths(repo_root)
    return {
        path
        for path in collectable
        if _TREE_SCAN_FILE_PATTERN.fullmatch(Path(path).name) is not None or path in TREE_SCAN_TESTS
    }


def build_import_reverse_map(repo_root: Path) -> dict[str, set[str]]:
    """Map runtime inputs to collectable tests through unpruned shared facts."""
    return build_impact(repo_root.resolve(), load_checkout(repo_root).collectable).tests_by_input


def load_checkout(repo_root: Path) -> Checkout:
    """Read the tree facts once so every path is classified against the same view."""
    repo_root = repo_root.resolve()
    hosts = pytest_test_hosts((repo_root / "pyproject.toml").read_text(encoding="utf-8"))
    collectable = frozenset(collectable_test_paths(repo_root))
    return Checkout(
        repo_root=repo_root,
        hosts=hosts,
        collectable=collectable,
        test_dirs=frozenset(d for path in collectable for d in _tests_directories(path)),
        global_files=frozenset(_plugin_files(repo_root)),
    )


def _tests_directories(path: str) -> list[str]:
    """Every ancestor directory of ``path`` that is named ``tests``."""
    parts = path.split("/")
    return [
        "/".join(parts[: index + 1]) for index, part in enumerate(parts[:-1]) if part == "tests"
    ]


def classify_path(path: str, checkout: Checkout) -> PathClass:
    """The single place that decides how a changed repo-relative path is handled.

    ``select_tests`` and the tracked-tree completeness test both call this, so
    the rules exist once. The first matching rule wins.
    """
    if _is_documentation_path(path, checkout.hosts):
        return PathClass.DOCUMENTATION
    if Path(path).name == "conftest.py":
        # Checked before existence: a deleted conftest still changes the fixtures
        # of every test below it, with no import to break.
        return PathClass.GLOBAL if path == "conftest.py" else PathClass.CONFTEST
    if not os.path.lexists(checkout.repo_root / path):
        return PathClass.DELETED
    if path in checkout.collectable:
        return PathClass.TEST
    return _repository_class(path, checkout)


def _repository_class(path: str, checkout: Checkout) -> PathClass:
    """The class of an existing, non-test, non-conftest path."""
    if path in _MANIFEST_FILES:
        return PathClass.MANIFEST
    if path in _GLOBAL_FILES or path in checkout.global_files:
        return PathClass.GLOBAL
    if path.startswith(_GLOBAL_PREFIXES):
        return PathClass.TREE_SCAN_ONLY if path.endswith(_PROSE_SUFFIXES) else PathClass.GLOBAL
    if path in _TREE_SCAN_ONLY_FILES or path.startswith(_TREE_SCAN_ONLY_PREFIXES):
        return PathClass.TREE_SCAN_ONLY
    root = path.split("/", maxsplit=1)[0]
    if root == "ui" and not path.endswith(".py"):
        return PathClass.FRONTEND
    if root in _PACKAGE_ROOTS and "/" in path:
        return PathClass.PACKAGE
    return PathClass.UNMAPPED


def package_tests(path: str, checkout: Checkout) -> set[str]:
    """Collectable tests of the package that owns ``path``.

    Walk up from the path's directory; the nearest ``tests`` directory that holds
    a collectable test (the directory itself when the path is inside one, else a
    sibling ``tests`` beside an ancestor) owns the path, and every collectable test
    below it is the package's test set.
    """
    directory = path.rpartition("/")[0]
    while True:
        tests_dir = (
            directory
            if directory.rpartition("/")[2] == "tests"
            else f"{directory}/tests".lstrip("/")
        )
        if tests_dir in checkout.test_dirs:
            return {test for test in checkout.collectable if test.startswith(f"{tests_dir}/")}
        if not directory:
            return set()
        directory = directory.rpartition("/")[0]


def _conftest_tests(path: str, checkout: Checkout) -> set[str]:
    """The collectable tests a conftest.py can affect: its directory subtree."""
    prefix = path.rpartition("/")[0] + "/"
    return {test for test in checkout.collectable if test.startswith(prefix)}


def _reference_pattern(root: str) -> re.Pattern[str]:
    """A string-literal reference to a top-level directory: ``.github/`` or ``".github"``.

    The unquoted form must not follow a word character or a dot, so the module
    name ``base.agents`` does not reference ``.agents``.
    """
    escaped = re.escape(root)
    return re.compile(rf"(?<![\w.]){escaped}/|[\"']{escaped}[\"']")


def referencing_tests(root: str, checkout: Checkout) -> set[str]:
    """Collectable tests whose source text names the top-level directory ``root``."""
    pattern = _reference_pattern(root)
    return {
        test
        for test in checkout.collectable
        if pattern.search((checkout.repo_root / test).read_text(encoding="utf-8"))
    }


def _path_tests(
    path: str, path_class: PathClass, checkout: Checkout, reverse_map: dict[str, set[str]]
) -> set[str]:
    """What one changed path contributes to the candidate subset."""
    return _class_tests(path, path_class, checkout, reverse_map) | _doc_readers(path, checkout)


def _class_tests(
    path: str, path_class: PathClass, checkout: Checkout, reverse_map: dict[str, set[str]]
) -> set[str]:
    """The contribution of a path's class rule."""
    runtime = _runtime_tests(path, reverse_map)
    if path_class is PathClass.TEST:
        return {path} | runtime
    if path_class is PathClass.CONFTEST:
        return _conftest_tests(path, checkout) | runtime
    if path_class is PathClass.PACKAGE:
        return runtime | package_tests(path, checkout)
    if path_class is PathClass.MANIFEST:
        return runtime | _manifest_tests(path, checkout, reverse_map)
    root = path.split("/", maxsplit=1)[0]
    if path_class is PathClass.TREE_SCAN_ONLY and root in _REFERENCED_ROOTS:
        return referencing_tests(root, checkout) | runtime
    return runtime


def _manifest_tests(
    manifest: str, checkout: Checkout, reverse_map: dict[str, set[str]]
) -> set[str]:
    """The tests that can observe a root meizel manifest: meizel's own tests, every test whose
    source names the file, and the runtime and package tests of every source module that does.

    The readers are derived, not listed: a string constant equal to the file name in any
    Python file under the code tops, outside docstrings (a comment or docstring reads nothing)."""
    selected = {test for test in checkout.collectable if test.startswith("meizel/")}
    for file in python_files(checkout.repo_root):
        relative = file.relative_to(checkout.repo_root).as_posix()
        if not code_names(file, manifest, exact=True):
            continue
        if relative in checkout.collectable:
            selected.add(relative)
        elif not _is_test_dir_path(relative, checkout.hosts):
            selected |= _runtime_tests(relative, reverse_map) | package_tests(relative, checkout)
    return selected


def _doc_readers(path: str, checkout: Checkout) -> set[str]:
    """Tests known to read skill or OKF documents at run time, when one of those changed.

    The unanchored-read rule does not count Markdown as data, so the readers that are known by
    name are pinned the way the tree-scan family is: every collectable test whose code holds
    ``SKILL.md`` (a skill) or ``.okf.md`` (an OKF document) as part of a string constant."""
    name = Path(path).name
    needle = "SKILL.md" if name == "SKILL.md" else ".okf.md" if name.endswith(".okf.md") else None
    if needle is None:
        return set()
    return {
        test
        for test in checkout.collectable
        if code_names(checkout.repo_root / test, needle, exact=False)
    }


def _runtime_tests(path: str, reverse_map: dict[str, set[str]]) -> set[str]:
    return {
        test
        for parent in (Path(path), *Path(path).parents)
        for test in reverse_map.get(parent.as_posix(), ())
    }


def documentation_runtime_tests(
    changed_files: list[str], *, repo_root: Path, base_ref: str | None = None
) -> frozenset[str]:
    """Known readers of documentation inputs in either tree, without executing tests.

    Unknown inputs alone cannot establish a documentation dependency. Once a known
    reader requests backend CI, the selector still reports its incomplete evidence.
    """
    if not any(is_doc_path(path) for path in changed_files):
        return frozenset()
    checkout = load_checkout(repo_root)
    paths = [path for path in changed_files if _is_documentation_path(path, checkout.hosts)]
    if not paths:
        return frozenset()
    impact = build_impact(repo_root, checkout.collectable)
    selected = {test for path in paths for test in _runtime_tests(path, impact.tests_by_input)}
    if base_ref is not None:
        with base_checkout(repo_root, base_ref) as base_root:
            base = load_checkout(base_root)
            old = build_impact(base_root, base.collectable & checkout.collectable)
            selected.update(
                test for path in paths for test in _runtime_tests(path, old.tests_by_input)
            )
    return frozenset(selected)


def _owner_tests(
    changed: dict[str, PathClass], checkout: Checkout, reverse_map: dict[str, set[str]]
) -> set[str]:
    """The union of what each changed path contributes (rules by class)."""
    selected: set[str] = set()
    for path, path_class in changed.items():
        selected.update(_path_tests(path, path_class, checkout, reverse_map))
    return selected


# meiva:note
# Only an impact that cannot be computed returns FULL: a queue branch or non-PR event, a global,
# unmapped or deleted-without-base path. A large impact is still SELECTED, because the merge
# queue runs the full suite before anything reaches main; the estimates only report and size.
# meiva:link docs/conventions/engineering/test-selection.md
def select_tests(
    changed_files: list[str],
    *,
    repo_root: Path,
    event: str = "pull_request",
    head_ref: str = "",
    base_ref: str | None = None,
) -> SelectionResult:
    """Apply the ordered conservative test-selection rules to one changed-file list."""
    repo_root = repo_root.resolve()
    changed = tuple(sorted({path.strip() for path in changed_files if path.strip()}))
    checkout = load_checkout(repo_root)
    durations = load_durations(repo_root / ".test_durations")
    full_estimate = estimate_seconds(checkout.collectable, durations)

    if event != "pull_request" or head_ref.startswith(_QUEUE_PREFIXES):
        return _result("FULL", "queue-or-non-pr", full_estimate=full_estimate)
    if all(
        _is_documentation_path(path, checkout.hosts) for path in changed
    ) and not documentation_runtime_tests(list(changed), repo_root=repo_root, base_ref=base_ref):
        return _result("SKIP", "docs-only", full_estimate=full_estimate)

    local = _local_config_paths(changed, checkout, base_ref)
    classes = _classify_changed(changed, checkout, local)
    forced = _forced_full(classes, full_estimate)
    if forced is not None:
        return forced

    candidates = _runtime_candidates(classes, checkout, base_ref, full_estimate, local)
    if isinstance(candidates, SelectionResult):
        return candidates
    candidate_tests, source_count, diagnostics = candidates
    # Tree-scan tests are unreachable through the owner rules; pin them so a
    # SELECTED run keeps the repo-wide gates (task #4183).
    tests = tuple(sorted((candidate_tests | tree_scan_tests(repo_root)) & checkout.collectable))
    estimate = estimate_seconds(tests, durations, reference_paths=set(checkout.collectable))
    return _result(
        "SELECTED",
        "owner-tests",
        tests=tests,
        est_seconds=estimate,
        full_estimate=full_estimate,
        map_source_count=source_count,
        diagnostics=diagnostics,
        shards=subset_shard_count(len(tests), estimate, full_estimate, len(checkout.collectable)),
    )


def _classify_changed(
    paths: tuple[str, ...], checkout: Checkout, local: frozenset[str]
) -> dict[str, PathClass]:
    classes: dict[str, PathClass] = {}
    for path in paths:
        kind = classify_path(path, checkout)
        classes[path] = (
            PathClass.TREE_SCAN_ONLY if path in local and kind is PathClass.GLOBAL else kind
        )
    return classes


# meiva:note
# The changed global paths whose change is confined to configuration no test run loads.
def _local_config_paths(
    changed: tuple[str, ...], checkout: Checkout, base_ref: str | None
) -> frozenset[str]:
    if "pyproject.toml" in changed and pyproject_change_is_local(checkout.repo_root, base_ref):
        return frozenset({"pyproject.toml"})
    return frozenset()


# meiva:note
# Unions both trees' runtime impact. A test whose dependency graph holds an input the analysis
# cannot bound may read any changed path, so every subset includes it. The result is the candidate
# tests (the changed paths' consumers plus those tests), the map size and the visible
# diagnostics, or the FULL result for a deleted path without base facts.
def _runtime_candidates(
    classes: dict[str, PathClass],
    checkout: Checkout,
    base_ref: str | None,
    full_estimate: float,
    local: frozenset[str],
) -> tuple[set[str], int, tuple[str, ...]] | SelectionResult:
    impact = build_impact(checkout.repo_root, checkout.collectable)
    reverse_map = impact.tests_by_input
    data_changed = any(_is_data_change(path, kind) for path, kind in classes.items())
    diagnostics = list(
        unknown_diagnostics(blocking_gaps(impact, data_changed=data_changed), tree="head")
    )
    tainted = unbounded_tests(impact, data_changed=data_changed)
    selected = _owner_tests(classes, checkout, reverse_map)
    if base_ref is not None:
        with base_checkout(checkout.repo_root, base_ref) as base_root:
            base = load_checkout(base_root)
            old_impact = build_impact(base_root, base.collectable & checkout.collectable)
            diagnostics.extend(
                unknown_diagnostics(
                    blocking_gaps(old_impact, data_changed=data_changed), tree="base"
                )
            )
            tainted.update(unbounded_tests(old_impact, data_changed=data_changed))
            old_classes = _classify_changed(tuple(classes), base, local)
            old_forced = _forced_full(old_classes, full_estimate)
            if old_forced is not None:
                return old_forced
            selected.update(_owner_tests(old_classes, base, old_impact.tests_by_input))
            for path, tests_for_path in old_impact.tests_by_input.items():
                reverse_map.setdefault(path, set()).update(tests_for_path & checkout.collectable)
    elif any(kind is PathClass.DELETED for kind in classes.values()):
        # A removed dependency is only visible in the base tree: without it no
        # subset can name the tests that lost an input.
        return _result(
            "FULL",
            "incomplete-impact",
            full_estimate=full_estimate,
            map_source_count=len(reverse_map),
            diagnostics=tuple(
                sorted(
                    {
                        *diagnostics,
                        *(
                            f"head:{path}: deleted path requires --base-ref to recover runtime impact"
                            for path, kind in classes.items()
                            if kind is PathClass.DELETED
                        ),
                    }
                )
            ),
        )
    return selected | tainted, len(reverse_map), tuple(sorted(set(diagnostics)))


# meiva:note
# Whether a changed path is a data file whose readers the analysis may have missed: not Python,
# Markdown (prose; the skill and OKF readers are pinned), a declaration or baseline, or a path in
# a class that names its readers. The merge queue runs the full suite.
# meiva:link docs/conventions/engineering/test-selection.md
def _is_data_change(path: str, kind: PathClass) -> bool:
    modeled = (
        path.endswith((".py", ".md"))
        or Path(path).name in _DECLARATION_NAMES
        or path.startswith(_BASELINE_PREFIXES)
        or path in _BASELINE_FILES
        or kind in _MODELED_CLASSES
    )
    return not modeled


def _forced_full(classes: dict[str, PathClass], full_estimate: float) -> SelectionResult | None:
    """FULL for a global path or, as the runtime safety net, an unowned path."""
    global_paths = tuple(path for path, kind in classes.items() if kind is PathClass.GLOBAL)
    if global_paths:
        return _result(
            "FULL",
            f"global-path:{global_paths[0]}",
            full_estimate=full_estimate,
            forced_roots=global_paths,
        )
    unmapped = tuple(path for path, kind in classes.items() if kind is PathClass.UNMAPPED)
    if unmapped:
        return _result("FULL", "unmapped", full_estimate=full_estimate, blind_changed=unmapped)
    return None


def main(argv: list[str] | None = None) -> int:
    """Run the selector CLI and print either JSON or a concise audit summary."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--changed-files", type=Path, required=True)
    parser.add_argument("--head-ref", default="")
    parser.add_argument("--event", default="pull_request")
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--base-ref", help="Committed merge-base for removed dependencies and paths"
    )
    args = parser.parse_args(argv)

    try:
        changed_files = args.changed_files.read_text().splitlines()
    except OSError as error:
        parser.error(f"cannot read changed files: {error}")
    result = select_tests(
        changed_files,
        repo_root=args.repo_root,
        event=args.event,
        head_ref=args.head_ref,
        base_ref=args.base_ref,
    )
    for diagnostic in result.diagnostics:
        print(f"test-impact: {diagnostic}", file=sys.stderr)
    if args.json:
        print(json.dumps(result.as_json(), sort_keys=True))
    else:
        print(f"selector mode={selection_mode()} decision={result.decision} reason={result.reason}")
        print(f"changed files={args.changed_files}")
        print(
            f"tests={result.count} estimate={result.est_seconds:.3f}s full={result.full_est_seconds:.3f}s"
        )
        print(
            f"map sources={result.map_source_count} blind={','.join(result.blind_changed) or '-'}"
        )
    return 0


def _is_collectable_test_path(path: str) -> bool:
    return (
        not path.startswith("tests/e2e/")
        and Path(path).name != "conftest.py"
        and _TEST_FILE_PATTERN.fullmatch(Path(path).name) is not None
    )


def _is_documentation_path(path: str, hosts: tuple[str, ...]) -> bool:
    return (
        not path.startswith(_NON_DOCUMENTATION_PREFIXES)
        and not _is_test_dir_path(path, hosts)
        and is_doc_path(path)
    )


def _plugin_modules(repo_root: Path) -> list[str]:
    """The dotted modules the root conftest lists in ``pytest_plugins``."""
    conftest = repo_root / "conftest.py"
    if not conftest.is_file():
        return []
    return list(plugin_modules(ast.parse(conftest.read_text(), filename=str(conftest))))


def _plugin_files(repo_root: Path) -> set[str]:
    """Repo files every pytest process loads: plugin modules and their package inits.

    A module that does not resolve to a repo file (an installed plugin such as
    ``pytester``) has no path to change here and is skipped.
    """
    index = ModuleIndex(repo_root)
    return {file for module in _plugin_modules(repo_root) for file in module_files(module, index)}


def load_durations(path: Path) -> dict[str, float]:
    """Load measured pytest node durations; an absent timing file has no measurements."""
    if not path.is_file():
        return {}
    data = cast(dict[object, object], json.loads(path.read_text()))
    if not isinstance(data, dict):
        raise TypeError(f"duration file must contain a JSON object: {path}")
    durations: dict[str, float] = {}
    for node_id, seconds in data.items():
        if (
            isinstance(node_id, str)
            and isinstance(seconds, (int, float))
            and not isinstance(seconds, bool)
        ):
            durations[node_id] = float(seconds)
    return durations


def estimate_seconds(
    test_paths: set[str] | frozenset[str] | tuple[str, ...],
    durations: dict[str, float],
    *,
    reference_paths: set[str] | None = None,
) -> float:
    """Estimate selected files using the reference population's average for unmeasured files."""
    reference = test_paths if reference_paths is None else reference_paths
    ordered_reference = tuple(sorted(reference))
    ordered_tests = tuple(sorted(test_paths))
    reference_set = set(reference)
    # Group once: the complete timing model has an entry for every measured
    # node, so scanning it again for each file makes selection unnecessarily slow.
    by_file: dict[str, list[float]] = {}
    for node_id, seconds in durations.items():
        test_path, separator, _ = node_id.partition("::")
        if separator and test_path in reference_set:
            by_file.setdefault(test_path, []).append(seconds)
    known_entries = [
        seconds for test_path in ordered_reference for seconds in by_file.get(test_path, ())
    ]
    average = sum(known_entries) / len(known_entries) if known_entries else 0.0
    return sum(
        sum(by_file[test_path]) if test_path in by_file else average for test_path in ordered_tests
    )


# meiva:note
# The shards a SELECTED subset runs on: its share of the suite in units of one full shard, by
# estimated time (by file count without timings), rounded up, at most the fan-out and the files.
def subset_shard_count(tests: int, estimate: float, full_estimate: float, collectable: int) -> int:
    share = estimate / full_estimate if full_estimate > 0 else tests / max(collectable, 1)
    return max(1, min(FULL_FANOUT_SHARDS, tests, math.ceil(round(share * FULL_FANOUT_SHARDS, 6))))


def _result(
    decision: str,
    reason: str,
    *,
    tests: tuple[str, ...] = (),
    est_seconds: float = 0.0,
    full_estimate: float,
    blind_changed: tuple[str, ...] = (),
    forced_roots: tuple[str, ...] = (),
    map_source_count: int = 0,
    diagnostics: tuple[str, ...] = (),
    shards: int = 0,
) -> SelectionResult:
    return SelectionResult(
        decision=decision,
        reason=reason,
        tests=tests,
        est_seconds=est_seconds,
        full_est_seconds=full_estimate,
        blind_changed=blind_changed,
        forced_roots=forced_roots,
        map_source_count=map_source_count,
        diagnostics=diagnostics,
        shards=shards,
    )


if __name__ == "__main__":
    raise SystemExit(main())
