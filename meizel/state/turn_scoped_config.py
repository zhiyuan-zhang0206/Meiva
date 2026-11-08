# meiva:note
# The effect shell of the turn-scoped-config rule: it walks the scan directories, resolves and reads
# each file, then prints the report. Which files the rule skips, which reads are findings and the
# text of every heading are decided in `meizel.state.turn_scoped_config_plan`.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from meizel import allowances, lint_common
from meizel.checkout import GitRepo, source_tree
from meizel.graph.universe import SourceTree
from meizel.state import lint_paths
from meizel.state import turn_scoped_config_plan as plan

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]


# meiva:note
# What the policy declares for this rule, read once by the entry and handed down: the directories and
# extra files it walks, the files it exempts, the files that ARE the plugin-config mechanism (the
# registry owns the process-global map, the view layers an agent's overrides over it) and the config
# fields every file is judged by. Nothing here is read at import, so importing this module opens no
# file and a caller decides which policy the run is judged by.
@dataclass(frozen=True, slots=True)
class _Rule:
    scan_dirs: tuple[str, ...]
    extra_files: tuple[str, ...]
    allowed_files: frozenset[str]
    plugin_mechanism_files: frozenset[str]
    rule_inputs: tuple[str, ...]

    @classmethod
    def of(cls, policy: allowances.Policy) -> _Rule:
        section = "turn-scoped-config"
        return cls(
            policy.names(section, "scan-dirs"),
            policy.names(section, "extra-files"),
            frozenset(policy.reasons(section, "files")),
            frozenset(policy.reasons(section, "plugin-mechanism-files")),
            policy.names(section, "rule-inputs"),
        )


# meiva:note
# Every file the rule reads: explicit paths as given, otherwise each scan directory walked plus the
# policy's extra files.
def _iter_files(resolved: list[str], root: Path, *, explicit: bool, rule: _Rule) -> list[Path]:
    if explicit:
        return lint_paths.python_paths(root, resolved)
    files: list[Path] = []
    for d in rule.scan_dirs:
        files.extend((root / d).rglob("*.py"))
    files.extend(root / f for f in rule.extra_files)
    return files


def _report(heading: str, entries: list[str]) -> None:
    if not entries:
        return
    print(heading + "\n", file=sys.stderr)
    for entry in entries:
        print(f"  {entry}", file=sys.stderr)


# meiva:note
# Collect one file's bare, layered and process-global plugin config reads. The label is the resolved
# path, so a link into the scan scope is judged as what it points at, and the skip decisions are
# asked of the plan with both that label and the name the scan reached.
def _scan(
    tree: SourceTree,
    path: Path,
    per_agent: set[str],
    errors: list[str],
    layered_errors: list[str],
    plugin_errors: list[str],
    *,
    root: Path,
    rule: _Rule,
) -> None:
    rel = lint_paths.label(path.resolve(), root.resolve())
    if plan.skips(rel, path.name, allowed_files=rule.allowed_files):
        return
    text = lint_common.read_utf8_text(tree, path)
    if text is None:
        return
    layered_errors.extend(plan.layered_reads(text, rel, per_agent))
    errors.extend(plan.settings_reads(text, rel, per_agent))
    plugin_errors.extend(plan.plugin_config_reads(text, rel, rule.plugin_mechanism_files))


# meiva:note
# Run the lint; `per_agent_field_names` returns the config fields that are per-agent.
def main(
    argv: list[str],
    *,
    per_agent_field_names: Callable[[], Iterable[str]],
    repo_root: Path | None = None,
    policy_root: Path | None = None,
) -> int:
    root = _REPO_ROOT if repo_root is None else repo_root
    tree = source_tree(root)
    # meiva:note
    # Two checkouts, named apart: `repo_root` is the tree this run judges, `policy_root` is the one
    # whose policy says what the rule scans. They are the same checkout unless a caller says
    # otherwise, so the rule's scope is the configuration of the repository that runs it -- and a
    # caller judging a synthetic tree may judge it by a synthetic policy rather than this one.
    rule = _Rule.of(allowances.load(GitRepo(policy_root or _REPO_ROOT)))
    argv, only = lint_common.split_only(argv)
    resolved: list[str] = []
    if argv:
        resolved, missing = lint_common.resolve_targets(tree, argv)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    scope = lint_common.changed_scope(tree, only, inputs=rule.rule_inputs)
    per_agent = set(per_agent_field_names())
    errors: list[str] = []
    layered_errors: list[str] = []
    plugin_errors: list[str] = []
    files = _iter_files(resolved, root, explicit=bool(argv), rule=rule)
    for path in lint_common.restrict(files, scope, root):
        _scan(tree, path, per_agent, errors, layered_errors, plugin_errors, root=root, rule=rule)
    _report(plan.PLUGIN_HEADING, plugin_errors)
    _report(plan.LAYERED_HEADING, layered_errors)
    _report(plan.SINGLETON_HEADING, errors)
    return 1 if (errors or plugin_errors or layered_errors) else 0
