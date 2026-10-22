from __future__ import annotations

import re
import sys
from collections.abc import Callable, Iterable
from pathlib import Path

from meizel import allowances, lint_common

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]
_POLICY = allowances.load(_REPO_ROOT)

_SCAN_DIRS = _POLICY.names("turn-scoped-config", "scan-dirs")
_EXTRA_FILES = _POLICY.names("turn-scoped-config", "extra-files")
_ALLOWED_FILES = frozenset(_POLICY.reasons("turn-scoped-config", "files"))

# The files that ARE the plugin-config mechanism: the registry owns the process-global map, the
# view layers an agent's overrides over it.
_PLUGIN_MECHANISM_FILES = frozenset(_POLICY.reasons("turn-scoped-config", "plugin-mechanism-files"))

_SETTINGS_ATTR = re.compile(r"\bsettings\.([a-z_]+)\.([a-z_]+)")
# A per-agent field read through the model layering or `get_field` by its literal name; the first
# group is the field, the second the rest of the call (to see whether it names `overrides`).
_LAYERED_READ = re.compile(r"\b(?:resolve_setting|get_field)\(\s*[\"']([a-z_]+)[\"']([^)]*)\)")
# Subscript only — `name in _PLUGIN_CONFIGS` is a registration probe, not a read.
_PLUGIN_CONFIGS_READ = re.compile(r"\b_PLUGIN_CONFIGS\[")


def _iter_files(paths: list[str]) -> list[Path]:
    if paths:
        return [Path(p) for p in paths if p.endswith(".py")]
    files: list[Path] = []
    for d in _SCAN_DIRS:
        files.extend((_REPO_ROOT / d).rglob("*.py"))
    files.extend(_REPO_ROOT / f for f in _EXTRA_FILES)
    return files


# The config registry decides which fields are per-agent for every file.
_RULE_INPUTS = _POLICY.names("turn-scoped-config", "rule-inputs")


def _layered_reads(text: str, rel: str, per_agent: set[str]) -> list[str]:
    """`file:line: call` for each per-agent setting read by name without the agent's overrides."""
    return [
        f"{rel}:{text.count(chr(10), 0, m.start()) + 1}: {m.group(0).splitlines()[0].strip()}"
        for m in _LAYERED_READ.finditer(text)
        if m.group(1) in per_agent and "overrides" not in m.group(2)
    ]


def _report(heading: str, entries: list[str]) -> None:
    if not entries:
        return
    print(heading + "\n", file=sys.stderr)
    for entry in entries:
        print(f"  {entry}", file=sys.stderr)


def _scan(
    path: Path,
    per_agent: set[str],
    errors: list[str],
    layered_errors: list[str],
    plugin_errors: list[str],
) -> None:
    """Collect one file's bare, layered and process-global plugin config reads."""
    resolved = path.resolve()
    try:
        rel = resolved.relative_to(_REPO_ROOT).as_posix()
    except ValueError:
        rel = resolved.as_posix()
    if rel in _ALLOWED_FILES or "/tests/" in rel or rel.startswith("tests/"):
        return
    if path.name.startswith("test_") or path.name.endswith("_test.py"):
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return
    layered_errors.extend(_layered_reads(text, rel, per_agent))
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        for m in _SETTINGS_ATTR.finditer(line):
            if m.group(2) in per_agent:
                errors.append(f"{rel}:{lineno}: {line.strip()}")
        if rel not in _PLUGIN_MECHANISM_FILES and _PLUGIN_CONFIGS_READ.search(line):
            plugin_errors.append(f"{rel}:{lineno}: {line.strip()}")


def main(argv: list[str], *, per_agent_field_names: Callable[[], Iterable[str]]) -> int:
    """Run the lint; `per_agent_field_names` returns the config fields that are per-agent."""
    argv, only = lint_common.split_only(argv)
    missing = [a for a in argv if not Path(a).exists()]
    if missing:
        print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
        return 1
    scope = lint_common.changed_scope(only, _REPO_ROOT, inputs=_RULE_INPUTS)
    per_agent = set(per_agent_field_names())
    errors: list[str] = []
    layered_errors: list[str] = []
    plugin_errors: list[str] = []
    for path in lint_common.restrict(_iter_files(argv), scope, _REPO_ROOT):
        _scan(path, per_agent, errors, layered_errors, plugin_errors)
    _report(
        "plugin config read straight out of the process-global _PLUGIN_CONFIGS in turn-scoped "
        "code — use `get_plugin_config(<plugin>, slices)` (or `process_plugin_config` in the "
        "exec child); in hosted mode the map holds whichever agent booted the process:",
        plugin_errors,
    )
    _report(
        "per-agent setting resolved from the cluster value in turn-scoped code — pass the "
        "agent's `overrides` slice to `resolve_setting`, and read other per-agent fields "
        "from the slices:",
        layered_errors,
    )
    _report(
        "per-agent config read through the bare settings singleton in turn-scoped code — read "
        "it from the agent's slices or `agent_setting`; in hosted mode the singleton holds "
        "the CLUSTER default, not this agent's overlay:",
        errors,
    )
    return 1 if (errors or plugin_errors or layered_errors) else 0
