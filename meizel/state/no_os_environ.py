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
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from meizel import allowances, lint_common
from meizel.checkout import GitRepo, roles_at, source_tree
from meizel.graph.universe import SourceTree
from meizel.roles import select_packages
from meizel.state import lint_paths
from meizel.state import no_os_environ_plan as plan

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]


# meiva:note
# Everything this rule takes from the repository, read once by the entry and handed down: the scan
# directories (frameworks whose production code the default scan skips, with their own test files
# read), the provider keys, the variables read from the live environment by design, the test files
# that are the settings-free boundary and the registry-owned names Rule 2 takes as input. Every one
# of them is a policy entry listed in `meizel.policy.toml` under `[no-os-environ]` with its reason.
#
# Nothing here is read at import: importing the rule opens no file, and a run judged against another
# checkout is judged by that checkout's policy and roles.
@dataclass(frozen=True, slots=True)
class _Rule:
    scan_targets: tuple[str, ...]
    test_only_dirs: tuple[str, ...]
    provider_keys: frozenset[str]
    live_reads: frozenset[str]
    allowed_files: frozenset[str]
    rule_inputs: tuple[str, ...]

    @classmethod
    def of(cls, policy: allowances.Policy, roles: Mapping[str, tuple[str, ...]]) -> _Rule:
        section = "no-os-environ"
        test_only_dirs = policy.names(section, "test-only-dirs")
        return cls(
            plan.scan_targets(
                select_packages(roles, "application", "standalone"),
                select_packages(roles, "tooling"),
                test_only_dirs,
            ),
            test_only_dirs,
            frozenset(policy.reasons(section, "provider-key-vars")),
            frozenset(policy.reasons(section, "live-read-vars")),
            frozenset(policy.reasons(section, "files")),
            policy.names(section, "rule-inputs"),
        )


# meiva:note
# Every file the default scan reads: each scan target walked, a file under a skipped directory's
# production code dropped, and the `--only` changed files narrowing the result when `scope` names
# them. Explicit paths are read as given.
def _files_to_scan(
    tree: SourceTree,
    resolved: list[str],
    scope: frozenset[str] | None,
    root: Path,
    *,
    explicit: bool,
    rule: _Rule,
) -> list[Path]:
    if explicit:
        return lint_paths.python_paths(root, resolved)
    default = [
        path
        for path in lint_common.python_files(tree, [root / name for name in rule.scan_targets])
        if plan.in_default_scope(path, root, test_only_dirs=rule.test_only_dirs)
    ]
    return lint_common.restrict(default, scope, root)


# meiva:note
# One file's findings as (line number, stripped line, kind): a judged file is read, and an unreadable
# entry (a dangling symlink, binary content) keeps the shared lint contract of skipping.
def _scan_file(
    tree: SourceTree, path: Path, managed_envs: frozenset[str], rule: _Rule
) -> list[tuple[int, str, str]]:
    text = lint_common.read_utf8_text(tree, path)
    if text is None:
        return []
    return plan.setenv_violations(
        text,
        managed_envs=managed_envs,
        provider_keys=rule.provider_keys,
        live_reads=rule.live_reads,
    )


# meiva:note
# Run the lint; `managed_aliases` returns the env names the config registry owns.
def main(
    argv: list[str],
    *,
    managed_aliases: Callable[[], frozenset[str]],
    repo_root: Path | None = None,
    policy_root: Path | None = None,
) -> int:
    root = repo_root if repo_root is not None else _REPO_ROOT
    tree = source_tree(root)
    # meiva:note
    # Two checkouts, named apart: `repo_root` is the tree this run judges, `policy_root` is the one
    # whose policy and roles say which directories the rule scans. They are the same checkout unless
    # a caller says otherwise, so a test judging a synthetic tree can hand it a synthetic policy.
    configured = policy_root if policy_root is not None else _REPO_ROOT
    rule = _Rule.of(allowances.load(GitRepo(configured)), roles_at(configured))
    argv, only = lint_common.split_only(argv)
    # meiva:note
    # argv non-empty = explicit paths; empty = the test files of all _SCAN_TARGETS, or the `--only`
    # changed files (the commit hook) under that same scope.
    resolved: list[str] = []
    if argv:
        resolved, missing = lint_common.resolve_targets(tree, argv)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    scope = lint_common.changed_scope(tree, only, inputs=rule.rule_inputs)
    py_files = _files_to_scan(tree, resolved, scope, root, explicit=bool(argv), rule=rule)
    judged = [
        path for path in py_files if plan.judges_file(path, root, allowed_files=rule.allowed_files)
    ]
    # meiva:note
    # Importing the config registry is the slow part of a run, so it is read once and only when the
    # scan has a file to judge.
    managed_envs = managed_aliases() if judged else frozenset[str]()

    total_violations = 0
    for path in sorted(judged):
        for lineno, content, kind in _scan_file(tree, path, managed_envs, rule):
            total_violations += 1
            print(plan.finding(path, root, lineno, kind))
            print(f"    {content}")

    if total_violations:
        print(plan.summary(total_violations), file=sys.stderr)
        return 1
    return 0
