# meiva:note
# Tests do not set an environment variable that `Settings` models.
#
# `Settings` is built once, so `monkeypatch.setenv` on a variable it owns changes nothing the code
# under test reads. A test sets the field on the settings object instead. Reading the environment
# from production code is not judged here: that is the `ambient-read` rule of `meizel check`, where
# the files that may read it are declared under `[roots]`.

from __future__ import annotations

import re
import sys
from collections.abc import Callable
from pathlib import Path

from meizel import allowances, lint_common
from meizel.roles import packages

__all__ = ["main"]

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Everything below is listed in meizel.policy.toml under [no-os-environ], each entry with its reason.
_POLICY = allowances.load(_REPO_ROOT)

# meiva:note
# Framework dirs whose production code the default scan skips; their own test files are read.
_TEST_ONLY_DIRS = _POLICY.names("no-os-environ", "test-only-dirs")

# Scan directories — only OUR code, do not scan .venv / vendor / node_modules.
_SCAN_DIRS = (
    *(d for d in packages("application") if d not in _TEST_ONLY_DIRS),
    *packages("tooling"),
)

_PROVIDER_KEY_ENV_VARS = frozenset(_POLICY.reasons("no-os-environ", "provider-key-vars"))

# Vars read from the live environment BY DESIGN (not via the Settings singleton, which holds the
# environment as it stood when it was built): in tests, monkeypatch.setenv on them is a REAL seam,
# the same class as the provider keys above. Every entry must have its reader file in _ALLOWED_FILES.
_LIVE_READ_ENV_VARS = frozenset(_POLICY.reasons("no-os-environ", "live-read-vars"))

# meiva:note
# Test files that exercise the settings-free boundary, where the environment is the actual input.
_ALLOWED_FILES = frozenset(_POLICY.reasons("no-os-environ", "files"))

# Test files — pytest fixtures mocking env is a legitimate use
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# Match only `monkeypatch.setenv("X"...)` / `monkeypatch.delenv("X"...)` —
# pytest fixture calls, which always happen after Settings import (inside
# the test function). Deliberately does **not** match `os.environ["X"] = ...`
# — some env in conftest is bootstrap before Settings import, or used for
# subprocesses (gateway / agent fork); neither is the settings-singleton bug.
_TEST_SETENV_PATTERN = re.compile(
    r"\bmonkeypatch\.(?:setenv|delenv)\s*\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]"
)

_TRIPLE_QUOTE_PATTERN = re.compile(r'"""|\'\'\'')


def _is_test_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _TEST_PATTERNS)


# meiva:note
# Return error list [(lineno, line_stripped, error_kind), ...].
#
# error_kind = "setenv-managed:<name>".
def _scan_file(
    path: Path, rel_path: str, managed_envs: frozenset[str]
) -> list[tuple[int, str, str]]:
    if rel_path in _ALLOWED_FILES or not _is_test_file(rel_path):
        return []
    text = lint_common.read_utf8_text(path)
    if text is None:
        return []  # unreadable entry (e.g. a dangling symlink) or binary content
    violations: list[tuple[int, str, str]] = []
    inside_triple = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        # Triple-quote state machine: count occurrences of """ / ''' per
        # line; odd count flips the inside state. We do not parse nested
        # string semantics, but this suffices (docstrings not written on a
        # single line `"""..."""` have all middle lines inside the triple range).
        quote_count = len(_TRIPLE_QUOTE_PATTERN.findall(line))
        line_starts_inside = inside_triple
        if quote_count % 2:
            inside_triple = not inside_triple
        # Entire line in docstring range (both start and end inside) -> skip
        if line_starts_inside and inside_triple:
            continue
        # `#` splits the line into code and comment; only check the code part.
        code, _, _ = line.partition("#")
        for m in _TEST_SETENV_PATTERN.finditer(code):
            env_name = m.group(1)
            if (
                env_name in managed_envs
                and env_name not in _PROVIDER_KEY_ENV_VARS
                and env_name not in _LIVE_READ_ENV_VARS
            ):
                violations.append((lineno, line.strip(), f"setenv-managed:{env_name}"))
    return violations


def _files_to_scan(
    resolved: list[str], scope: frozenset[str] | None, root: Path, *, explicit: bool
) -> list[Path]:
    """Explicit paths as given; otherwise every file the default scan reads, narrowed to the
    `--only` changed files when `scope` names them."""
    if explicit:
        return [root / entry for entry in resolved if entry.endswith(".py")]
    targets = [root / d for d in (*_SCAN_DIRS, "tests", *_TEST_ONLY_DIRS)]
    default = [p for target in targets for p in target.rglob("*.py") if _in_default_scope(p, root)]
    return lint_common.restrict(default, scope, root)


def _rel_label(path: Path, root: Path) -> str:
    """Repo-relative posix path, or the absolute path of a file outside the repo."""
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# The config registry decides which env names Rule 2 treats as Settings-managed, for every test.
_RULE_INPUTS = _POLICY.names("no-os-environ", "rule-inputs")


def _in_default_scope(path: Path, root: Path) -> bool:
    """Under a `_TEST_ONLY_DIRS` root only test files are scanned (Rule 2 only)."""
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return True
    if rel.split("/", 1)[0] in _TEST_ONLY_DIRS:
        return _is_test_file(rel)
    return True


def main(
    argv: list[str] | None = None,
    *,
    managed_aliases: Callable[[], frozenset[str]],
    repo_root: Path | None = None,
) -> int:
    """Run the lint; `managed_aliases` returns the env names the config registry owns."""
    root = repo_root if repo_root is not None else _REPO_ROOT
    argv = argv if argv is not None else sys.argv[1:]
    argv, only = lint_common.split_only(argv)
    # meiva:note
    # argv non-empty = explicit paths; empty = the test files of all _SCAN_DIRS + tests/, or
    # the `--only` changed files (the commit hook) under that same scope.
    resolved: list[str] = []
    if argv:
        resolved, missing = lint_common.resolve_targets(argv, root)
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
    scope = lint_common.changed_scope(only, root, inputs=_RULE_INPUTS)
    py_files = _files_to_scan(resolved, scope, root, explicit=bool(argv))
    # meiva:note
    # Importing the config registry is the slow part of a run that judges no test file.
    managed_envs: frozenset[str] = (
        managed_aliases()
        if any(_is_test_file(_rel_label(path, root)) for path in py_files)
        else frozenset[str]()
    )

    total_violations = 0
    for path in sorted(py_files):
        rel = _rel_label(path, root)
        violations = _scan_file(path, rel, managed_envs)
        for lineno, content, kind in violations:
            total_violations += 1
            env = kind.split(":", 1)[1]
            print(
                f"{rel}:{lineno}: monkeypatch.setenv/delenv on `{env}` — Settings is a singleton "
                f"built once; env changes do not reach the settings instance field. Use "
                f'`monkeypatch.setattr(settings, "<field_name>", value)` instead.'
            )
            print(f"    {content}")

    if total_violations:
        print(
            f"\n{total_violations} violations total. See the docstring at the top of "
            "meizel/state/no_os_environ.py for the exemption procedure and the rationale "
            "for using setattr in tests.",
            file=sys.stderr,
        )
        return 1
    return 0
