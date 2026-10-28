# meiva:note
# What the `no_os_environ` rule decides, over the values its shell read: whether a walked path is in
# the default scan, whether any scanned path is a test file, whether the rule judges one file, which
# of a file's lines set an environment variable the settings singleton models, and the text of a
# finding and of the summary. The shell locates the repository, reads the policy, walks the scan
# directories, reads each judged file and prints.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

import re
from collections.abc import Collection, Iterable
from pathlib import Path

__all__ = [
    "any_test_file",
    "finding",
    "in_default_scope",
    "judges_file",
    "setenv_violations",
    "summary",
]

# meiva:note
# What counts as a test file, because the bug is a test writing to a variable the settings singleton
# already holds: anything under a tests directory, and a module named `test_*.py` or `*_test.py`.
_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

# meiva:note
# Match only `monkeypatch.setenv("X"...)` / `monkeypatch.delenv("X"...)` — pytest fixture calls,
# which always happen after Settings import (inside the test function). Deliberately does **not**
# match `os.environ["X"] = ...`: some env in conftest is bootstrap before Settings import, or used
# for subprocesses (gateway / agent fork); neither is the settings-singleton bug.
_TEST_SETENV_PATTERN = re.compile(
    r"\bmonkeypatch\.(?:setenv|delenv)\s*\(\s*['\"]([A-Z_][A-Z0-9_]*)['\"]"
)

_TRIPLE_QUOTE_PATTERN = re.compile(r'"""|\'\'\'')


def _is_test_file(rel_path: str) -> bool:
    return any(pattern.search(rel_path) for pattern in _TEST_PATTERNS)


# meiva:note
# How a path is labelled in a finding: repository-relative when it is inside the repository, its
# absolute posix form when it is not.
def _label(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return path.as_posix()


# meiva:note
# Whether a walked path is inside the default scan: under a directory whose production code the scan
# skips, only its test files are read, and a path outside the repository is not narrowed at all.
def in_default_scope(path: Path, root: Path, *, test_only_dirs: Collection[str]) -> bool:
    try:
        rel = path.relative_to(root).as_posix()
    except ValueError:
        return True
    if rel.split("/", 1)[0] in test_only_dirs:
        return _is_test_file(rel)
    return True


# meiva:note
# Whether any scanned path is a test file: the shell only pays for the config registry's import when
# one is, so a run over production files alone stays fast.
def any_test_file(paths: Iterable[Path], root: Path) -> bool:
    return any(_is_test_file(_label(path, root)) for path in paths)


# meiva:note
# Whether the rule judges a file at all: only a test file it has not exempted, because the bug is a
# test writing to a variable the settings singleton already holds.
def judges_file(path: Path, root: Path, *, allowed_files: Collection[str]) -> bool:
    rel = _label(path, root)
    return rel not in allowed_files and _is_test_file(rel)


# meiva:note
# The violations of one file's text, as (line number, stripped line, kind). A line inside a
# docstring is not code, and only the part before a `#` is scanned, so a comment that names
# `monkeypatch.setenv` is not a finding. The kind is `setenv-managed:<name>`: a variable the
# settings singleton models, that is neither a provider key read from the live environment by
# design nor one of the variables a test may really set.
def setenv_violations(
    text: str,
    *,
    managed_envs: Collection[str],
    provider_keys: Collection[str],
    live_reads: Collection[str],
) -> list[tuple[int, str, str]]:
    violations: list[tuple[int, str, str]] = []
    inside_triple = False
    for lineno, line in enumerate(text.splitlines(), start=1):
        quote_count = len(_TRIPLE_QUOTE_PATTERN.findall(line))
        line_starts_inside = inside_triple
        if quote_count % 2:
            inside_triple = not inside_triple
        if line_starts_inside and inside_triple:
            continue
        code, _, _ = line.partition("#")
        for match in _TEST_SETENV_PATTERN.finditer(code):
            env_name = match.group(1)
            if (
                env_name in managed_envs
                and env_name not in provider_keys
                and env_name not in live_reads
            ):
                violations.append((lineno, line.strip(), f"setenv-managed:{env_name}"))
    return violations


# meiva:note
# The line the rule prints for one violation: where it is, which variable, why the write does not
# reach the code under test, and what to do instead.
def finding(path: Path, root: Path, lineno: int, kind: str) -> str:
    env = kind.split(":", 1)[1]
    return (
        f"{_label(path, root)}:{lineno}: monkeypatch.setenv/delenv on `{env}` — Settings is a "
        f"singleton built once; env changes do not reach the settings instance field. Use "
        f'`monkeypatch.setattr(settings, "<field_name>", value)` instead.'
    )


# meiva:note
# The closing line of a run that found violations, naming where the exemption procedure is written.
def summary(total: int) -> str:
    return (
        f"\n{total} violations total. See the note at the top of "
        "meizel/state/no_os_environ.py for the exemption procedure and the rationale "
        "for using setattr in tests."
    )
