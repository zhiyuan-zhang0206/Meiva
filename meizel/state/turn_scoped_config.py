# meiva:note
# The effect shell of the turn-scoped-config rule: it walks the scan directories, resolves and reads
# each file, then prints the report. Which files the rule skips, which reads are findings and the
# text of every heading are decided in `meizel.state.turn_scoped_config_plan`.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from meizel import allowances, lint_common
from meizel.state import lint_paths
from meizel.state import turn_scoped_config_plan as plan

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_POLICY = allowances.load(_REPO_ROOT)

_SCAN_DIRS = _POLICY.names("turn-scoped-config", "scan-dirs")
_EXTRA_FILES = _POLICY.names("turn-scoped-config", "extra-files")
_ALLOWED_FILES = frozenset(_POLICY.reasons("turn-scoped-config", "files"))

# meiva:note
# The files that ARE the plugin-config mechanism: the registry owns the process-global map, the view
# layers an agent's overrides over it.
_PLUGIN_MECHANISM_FILES = frozenset(_POLICY.reasons("turn-scoped-config", "plugin-mechanism-files"))


# meiva:note
# Every file the rule reads: explicit paths as given, otherwise each scan directory walked plus the
# policy's extra files.
def _iter_files(resolved: list[str], root: Path = _REPO_ROOT, *, explicit: bool) -> list[Path]:
    if explicit:
        return lint_paths.python_paths(root, resolved)
    files: list[Path] = []
    for d in _SCAN_DIRS:
        files.extend((root / d).rglob("*.py"))
    files.extend(root / f for f in _EXTRA_FILES)
    return files


# The config registry decides which fields are per-agent for every file.
_RULE_INPUTS = _POLICY.names("turn-scoped-config", "rule-inputs")


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
    path: Path,
    per_agent: set[str],
    errors: list[str],
    layered_errors: list[str],
    plugin_errors: list[str],
    *,
    root: Path = _REPO_ROOT,
) -> None:
    rel = lint_paths.label(path.resolve(), root.resolve())
    if plan.skips(rel, path.name, allowed_files=_ALLOWED_FILES):
        return
    text = lint_common.read_utf8_text(path)
    if text is None:
        return
    layered_errors.extend(plan.layered_reads(text, rel, per_agent))
    errors.extend(plan.settings_reads(text, rel, per_agent))
    plugin_errors.extend(plan.plugin_config_reads(text, rel, _PLUGIN_MECHANISM_FILES))


# meiva:note
# Run the lint; `per_agent_field_names` returns the config fields that are per-agent.
def main(
    argv: list[str],
    *,
    per_agent_field_names: Callable[[], Iterable[str]],
    repo_root: Path | None = None,
) -> int:
    root = _REPO_ROOT if repo_root is None else repo_root
    argv, only = lint_common.split_only(argv)
    resolved: list[str] = []
    if argv:
        resolved, missing = lint_common.resolve_targets(argv, root)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    scope = lint_common.changed_scope(only, root, inputs=_RULE_INPUTS)
    per_agent = set(per_agent_field_names())
    errors: list[str] = []
    layered_errors: list[str] = []
    plugin_errors: list[str] = []
    for path in lint_common.restrict(_iter_files(resolved, root, explicit=bool(argv)), scope, root):
        _scan(path, per_agent, errors, layered_errors, plugin_errors, root=root)
    _report(plan.PLUGIN_HEADING, plugin_errors)
    _report(plan.LAYERED_HEADING, layered_errors)
    _report(plan.SINGLETON_HEADING, errors)
    return 1 if (errors or plugin_errors or layered_errors) else 0
