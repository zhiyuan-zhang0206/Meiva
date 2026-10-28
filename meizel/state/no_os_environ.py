# meiva:note
# Tests do not set an environment variable that `Settings` models.
#
# `Settings` is built once, so `monkeypatch.setenv` on a variable it owns changes nothing the code
# under test reads. A test sets the field on the settings object instead. Reading the environment
# from production code is not judged here: that is the `ambient-read` rule of `meizel check`, where
# the files that may read it are declared under `[roots]`.
#
# This module is the effect shell of that rule: it locates the repository, reads the policy, walks
# the scan directories, reads each judged file and prints. Which directories and files it judges and
# what it prints are decided in `meizel.state.no_os_environ_plan`.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from meizel import allowances, lint_common
from meizel.roles import packages
from meizel.state import lint_paths
from meizel.state import no_os_environ_plan as plan

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Everything below is listed in meizel.policy.toml under [no-os-environ], each entry with its reason.
_POLICY = allowances.load(_REPO_ROOT)

# meiva:note
# Framework dirs whose production code the default scan skips; their own test files are read.
_TEST_ONLY_DIRS = _POLICY.names("no-os-environ", "test-only-dirs")

# Scan directories — only OUR code, do not scan .venv / vendor / node_modules.
_SCAN_TARGETS = plan.scan_targets(packages("application"), packages("tooling"), _TEST_ONLY_DIRS)

_PROVIDER_KEY_ENV_VARS = frozenset(_POLICY.reasons("no-os-environ", "provider-key-vars"))

# Vars read from the live environment BY DESIGN (not via the Settings singleton, which holds the
# environment as it stood when it was built): in tests, monkeypatch.setenv on them is a REAL seam,
# the same class as the provider keys above. Every entry must have its reader file in _ALLOWED_FILES.
_LIVE_READ_ENV_VARS = frozenset(_POLICY.reasons("no-os-environ", "live-read-vars"))

# meiva:note
# Test files that exercise the settings-free boundary, where the environment is the actual input.
_ALLOWED_FILES = frozenset(_POLICY.reasons("no-os-environ", "files"))


# meiva:note
# Every file the default scan reads: each scan target walked, a file under a skipped directory's
# production code dropped, and the `--only` changed files narrowing the result when `scope` names
# them. Explicit paths are read as given.
def _files_to_scan(
    resolved: list[str], scope: frozenset[str] | None, root: Path, *, explicit: bool
) -> list[Path]:
    if explicit:
        return lint_paths.python_paths(root, resolved)
    default = [
        path
        for name in _SCAN_TARGETS
        for path in (root / name).rglob("*.py")
        if plan.in_default_scope(path, root, test_only_dirs=_TEST_ONLY_DIRS)
    ]
    return lint_common.restrict(default, scope, root)


# The config registry decides which env names Rule 2 treats as Settings-managed, for every test.
_RULE_INPUTS = _POLICY.names("no-os-environ", "rule-inputs")


# meiva:note
# One file's findings as (line number, stripped line, kind): a judged file is read, and an unreadable
# entry (a dangling symlink, binary content) keeps the shared lint contract of skipping. The registry
# is asked through `managed_envs` only here, so a scan that judges nothing never imports it.
def _scan_file(
    path: Path, root: Path, managed_envs: Callable[[], frozenset[str]]
) -> list[tuple[int, str, str]]:
    if not plan.judges_file(path, root, allowed_files=_ALLOWED_FILES):
        return []
    text = lint_common.read_utf8_text(path)
    if text is None:
        return []
    return plan.setenv_violations(
        text,
        managed_envs=managed_envs(),
        provider_keys=_PROVIDER_KEY_ENV_VARS,
        live_reads=_LIVE_READ_ENV_VARS,
    )


# meiva:note
# Run the lint; `managed_aliases` returns the env names the config registry owns.
def main(
    argv: list[str] | None = None,
    *,
    managed_aliases: Callable[[], frozenset[str]],
    repo_root: Path | None = None,
) -> int:
    root = repo_root if repo_root is not None else _REPO_ROOT
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    # meiva:note
    # argv non-empty = explicit paths; empty = the test files of all _SCAN_TARGETS, or the `--only`
    # changed files (the commit hook) under that same scope.
    resolved: list[str] = []
    if argv:
        resolved, missing = lint_common.resolve_targets(argv, root)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    scope = lint_common.changed_scope(only, root, inputs=_RULE_INPUTS)
    py_files = _files_to_scan(resolved, scope, root, explicit=bool(argv))
    # meiva:note
    # Importing the config registry is the slow part, so it is read at the first judged file and
    # never for a run that judges none.
    registry_envs: list[frozenset[str]] = []

    def managed_envs() -> frozenset[str]:
        if not registry_envs:
            registry_envs.append(managed_aliases())
        return registry_envs[0]

    total_violations = 0
    for path in sorted(py_files):
        for lineno, content, kind in _scan_file(path, root, managed_envs):
            total_violations += 1
            print(plan.finding(path, root, lineno, kind))
            print(f"    {content}")

    if total_violations:
        print(plan.summary(total_violations), file=sys.stderr)
        return 1
    return 0
