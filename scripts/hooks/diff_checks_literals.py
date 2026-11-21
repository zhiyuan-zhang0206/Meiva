# meiva:note
# Extract distinctive deleted literals from production-code diffs and scan candidate test files so
# `--skip-tests` still catches small sets of literal-assertion regressions without ever triggering a
# wide local pytest sweep. Kept pure of subprocesses and environment reads; `diff_checks.py` reads
# the git diff and runs pytest when the hit set sits within `MAX_LITERAL_AUTO_TESTS`.

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.hooks.diff_checks_pytest import PytestCounts

# meiva:note
# Hard upper bound on test files that `--skip-tests` may auto-execute when deleted literals match.
# Keeping this at 10 upholds the local no-wide-pytest invariant (a broad refactor can never stall
# the workstation by launching dozens or hundreds of test files) while catching small transitive
# literal-assertion sets such as PR #9123 (`9db9842748`, 2 files).
MAX_LITERAL_AUTO_TESTS = 10

# meiva:note
# Maximum hit paths listed in the stderr diagnostic when hits exceed `MAX_LITERAL_AUTO_TESTS`,
# keeping the warning readable on one screen while showing which suites matched.
OVER_CAP_PREVIEW_LIMIT = 5

# meiva:note
# Distinctiveness bounds for extracted literals: backtick tokens explicitly delimit code symbols or
# flags (>=5 chars drops trivial placeholders like `id` or `ok`); code-like quoted literals with
# structural punctuation need >=8 chars; plain space-separated prose needs >=16 chars and >=3 words
# so generic phrases like `"new message"` do not fan out across unrelated tests; unpunctuated single
# words need >=14 chars; strings above 120 chars are wrapped prose unlikely to match verbatim.
_MIN_BACKTICK_LENGTH = 5
_MIN_IDENTIFIER_LITERAL_LENGTH = 8
_MIN_PROSE_LITERAL_LENGTH = 16
_MIN_PROSE_WORDS = 3
_MIN_PLAIN_WORD_LENGTH = 14
_MAX_LITERAL_LENGTH = 120

# meiva:note
# Only long multi-word string literals (such as wrapped prompt sentences) are split on clause
# punctuation to recover distinctive sub-clauses like `"Unlike standalone Claude Code"`.
_MIN_CLAUSE_SPLIT_SOURCE_LENGTH = 35
_MIN_CLAUSE_LENGTH = 18

# meiva:note
# Cap extracted literals per diff so an unusually large file deletion stays fast (<1.5s) while
# scanning candidate tests.
_MAX_EXTRACTED_LITERALS = 200

# meiva:note
# Bounded fallback cap when `test_selector` reports blind files or cannot map a change: scanning at
# most 300 test files inside the changed production files' packages keeps wall-clock well under 1.5s
# and avoids sweeping the whole repository.
_MAX_FALLBACK_SCAN_FILES = 300

# meiva:note
# Skip unexpectedly huge generated fixture files (>1 MiB) during literal scanning; normal Python
# test modules are a few tens of KiB.
_MAX_TEST_FILE_BYTES = 1_048_576

_BACKTICK_RE = re.compile(r"`([^`\n]{4,120})`")
_STRING_RE = re.compile(
    r"""(?<!\\)(?:"([^"\\\n]*(?:\\.[^"\\\n]*)*)"|'([^'\\\n]*(?:\\.[^'\\\n]*)*)')"""
)
_ESCAPE_WHITESPACE_RE = re.compile(r"\\[nrt]")
_FSTRING_INTERP_RE = re.compile(r"\{[^{}]*\}")
_CLAUSE_SPLIT_RE = re.compile(r"[,;\u2014]|(?:\.\s+)")
_CODE_PUNCTUATION = (".", "_", "-", ":", "/", "=")
_SKIP_WALK_DIRS = frozenset(
    {".git", ".venv", ".worktrees", ".cache", "__pycache__", "node_modules", ".pytest_cache"}
)

# meiva:note
# Ubiquitous boilerplate literals that appear across hundreds of tests and carry no signal for a
# specific deleted line.
_STOP_LITERALS = frozenset(
    {
        "utf-8",
        "utf8",
        "ascii",
        "latin-1",
        "__main__",
        "__init__",
        "__all__",
        "__file__",
        "origin/main",
        "head",
        "stdout",
        "stderr",
        "stdin",
        "localhost",
        "127.0.0.1",
        "true",
        "false",
        "none",
        "null",
        "unknown",
        "error",
        "errors",
        "warning",
        "warnings",
        "default",
        "config",
        "status",
        "result",
        "results",
        "value",
        "values",
        "index",
        "select",
        "insert",
        "update",
        "delete",
        "where",
        "return",
        "import",
        "pytest",
        "python",
        "python3",
        "content",
        "message",
        "messages",
        "output",
        "input",
        "label",
        "timeout",
        "pending",
        "running",
        "failed",
        "passed",
        "skipped",
        "completed",
    }
)

_PASSED_RE = re.compile(r"^(\d+)tests\(([^)]+)\)$")
_TESTS_COUNT_RE = re.compile(r"^(\d+)tests$")


@dataclass(frozen=True)
class LiteralScan:
    literals: tuple[str, ...]
    hits: tuple[str, ...]
    scanned_files: int


def is_test_path(path: str) -> bool:
    parts = path.split("/")
    name = parts[-1]
    return (
        "tests" in parts[:-1]
        or name == "conftest.py"
        or name.startswith("test_")
        or name.endswith("_test.py")
    )


def is_pytest_file(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return name.endswith(".py") and (name.startswith("test_") or name.endswith("_test.py"))


def is_production_literal_source(path: str) -> bool:
    return bool(path) and not is_test_path(path) and path.endswith((".py", ".md"))


def _is_distinctive(candidate: str, *, from_backtick: bool) -> bool:
    text = candidate.strip()
    if len(text) > _MAX_LITERAL_LENGTH:
        return False
    if text.lower() in _STOP_LITERALS:
        return False
    if sum(ch.isalpha() for ch in text) < 3:
        return False
    if from_backtick:
        return len(text) >= _MIN_BACKTICK_LENGTH
    if text.startswith(("/", "./", "../", "http://", "https://")) and " " not in text:
        return len(text) >= 12
    has_code_punct = any(ch in text for ch in _CODE_PUNCTUATION)
    if " " in text:
        if not has_code_punct:
            return len(text) >= _MIN_PROSE_LITERAL_LENGTH and len(text.split()) >= _MIN_PROSE_WORDS
        return len(text) >= _MIN_IDENTIFIER_LITERAL_LENGTH
    if has_code_punct:
        return len(text) >= _MIN_IDENTIFIER_LITERAL_LENGTH
    return len(text) >= _MIN_PLAIN_WORD_LENGTH


def _clauses_from_chunk(chunk: str) -> list[str]:
    if len(chunk) < _MIN_CLAUSE_SPLIT_SOURCE_LENGTH or " " not in chunk:
        return []
    clauses: list[str] = []
    for raw_clause in _CLAUSE_SPLIT_RE.split(chunk):
        part = raw_clause.strip()
        if len(part) >= _MIN_CLAUSE_LENGTH and len(part.split()) >= _MIN_PROSE_WORDS:
            clauses.append(part)
    return clauses


def _string_literal_candidates(raw: str) -> list[str]:
    candidates: list[str] = []
    for segment in _FSTRING_INTERP_RE.split(raw):
        for chunk in _ESCAPE_WHITESPACE_RE.sub("\n", segment).splitlines():
            stripped = chunk.strip()
            if not stripped:
                continue
            candidates.append(stripped)
            candidates.extend(_clauses_from_chunk(stripped))
    return candidates


def _distinctive_literal(raw: str, *, from_backtick: bool) -> str | None:
    cleaned = _ESCAPE_WHITESPACE_RE.sub(" ", raw).strip()
    if cleaned and _is_distinctive(cleaned, from_backtick=from_backtick):
        return cleaned
    return None


def _line_literals(body: str, *, path: str) -> list[str]:
    is_python = path.endswith(".py")
    if is_python and body.lstrip().startswith("#"):
        return []
    extracted: list[str] = []
    for match in _BACKTICK_RE.finditer(body):
        literal = _distinctive_literal(match.group(1), from_backtick=True)
        if literal is not None:
            extracted.append(literal)
    if not is_python:
        return extracted
    for match in _STRING_RE.finditer(body):
        raw = match.group(1) if match.group(1) is not None else match.group(2)
        if raw is None:
            continue
        for candidate in _string_literal_candidates(raw):
            literal = _distinctive_literal(candidate, from_backtick=False)
            if literal is not None:
                extracted.append(literal)
    return extracted


# meiva:note
# Per-hunk subtraction keeps literals that genuinely vanished from a hunk — including literals moved
# to another section/hunk of the same file (the exact failure mode of PR #9123, where
# `meiva.agents.send_message` moved out of `_CLAUDE_CODE_VISIBILITY_WHY` into `_USER_ATTENTION_RULE`
# and broke `test_init_context_harness.py`) — while dropping literals that merely stayed put across
# an in-place refactor or re-indentation in the same hunk.
def extract_deleted_literals(diff_text: str) -> tuple[str, ...]:
    current_file = ""
    found: list[str] = []
    seen: set[str] = set()
    minus_literals: list[str] = []
    plus_lines: list[str] = []

    def flush_hunk() -> None:
        if not minus_literals:
            plus_lines.clear()
            return
        plus_blob = "\n".join(plus_lines)
        for literal in minus_literals:
            if len(found) >= _MAX_EXTRACTED_LITERALS:
                break
            if literal not in plus_blob and literal not in seen:
                seen.add(literal)
                found.append(literal)
        minus_literals.clear()
        plus_lines.clear()

    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            flush_hunk()
            current_file = ""
            continue
        if line.startswith("--- a/"):
            if not current_file:
                current_file = line[6:]
            continue
        if line.startswith("+++ b/"):
            current_file = line[6:]
            continue
        if line.startswith(("+++ /dev/null", "--- /dev/null")):
            continue
        if line.startswith("@@ "):
            flush_hunk()
            continue
        if not is_production_literal_source(current_file):
            continue
        if line.startswith("-"):
            minus_literals.extend(_line_literals(line[1:], path=current_file))
        elif line.startswith("+"):
            plus_lines.append(line[1:])

    flush_hunk()
    return tuple(found)


def _fallback_package_roots(changed_files: Sequence[str]) -> tuple[str, ...]:
    roots: list[str] = []
    seen: set[str] = set()
    for path in changed_files:
        if not is_production_literal_source(path):
            continue
        parts = path.split("/")
        if len(parts) < 2:
            continue
        top = parts[0]
        if top not in seen:
            seen.add(top)
            roots.append(top)
    return tuple(roots)


def _bounded_package_tests(
    root: Path,
    changed_files: Sequence[str],
    *,
    already: set[str],
    max_files: int = _MAX_FALLBACK_SCAN_FILES,
) -> None:
    for package_root in _fallback_package_roots(changed_files):
        base = root / package_root
        if not base.is_dir():
            continue
        for current, dirs, files in os.walk(base):
            dirs[:] = sorted(name for name in dirs if name not in _SKIP_WALK_DIRS)
            for name in sorted(files):
                if not is_pytest_file(name):
                    continue
                rel = (Path(current) / name).relative_to(root).as_posix()
                if rel in already:
                    continue
                already.add(rel)
                if len(already) >= max_files:
                    return


def candidate_test_files(
    root: Path,
    changed_files: Sequence[str],
    selection_tests: Sequence[str],
    *,
    blind: Sequence[str] = (),
    decision: str = "SELECTED",
    max_fallback_files: int = _MAX_FALLBACK_SCAN_FILES,
) -> tuple[str, ...]:
    if not root.is_dir():
        return ()
    seen: set[str] = set()
    for path in selection_tests:
        if is_pytest_file(path) and (root / path).is_file():
            seen.add(path)
    if blind or (not selection_tests and decision == "UNKNOWN"):
        _bounded_package_tests(root, changed_files, already=seen, max_files=max_fallback_files)
    return tuple(sorted(seen))


def find_literal_test_hits(
    root: Path,
    literals: Sequence[str],
    candidates: Sequence[str],
) -> LiteralScan:
    ordered_literals = tuple(literals)
    if not ordered_literals or not candidates or not root.is_dir():
        return LiteralScan(literals=ordered_literals, hits=(), scanned_files=0)
    matched: list[str] = []
    scanned = 0
    for relative in candidates:
        target = root / relative
        try:
            if not target.is_file() or target.stat().st_size > _MAX_TEST_FILE_BYTES:
                continue
            text = target.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        scanned += 1
        if any(literal in text for literal in ordered_literals):
            matched.append(relative)
    return LiteralScan(
        literals=ordered_literals, hits=tuple(sorted(matched)), scanned_files=scanned
    )


def format_literal_tests_field(
    hit_count: int,
    counts: PytestCounts,
    *,
    pytest_failed: bool,
) -> str:
    if counts.broken:
        return counts.values.get("tests", "broken(unknown)")
    passed_raw = counts.values.get("passed", "")
    passed_match = _PASSED_RE.match(passed_raw)
    passed_n = passed_match.group(1) if passed_match else "0"
    duration = passed_match.group(2) if passed_match else "0s"
    if not pytest_failed:
        return f"ok({hit_count}files,{passed_n}passed)({duration})"
    failed_match = _TESTS_COUNT_RE.match(counts.values.get("failed", ""))
    errors_match = _TESTS_COUNT_RE.match(counts.values.get("errors", ""))
    failed_n = failed_match.group(1) if failed_match else "0"
    errors_n = errors_match.group(1) if errors_match else "0"
    return f"failed({hit_count}files,{failed_n}failed,{errors_n}errors)({duration})"


def format_over_cap_notice(
    hits: Sequence[str],
    *,
    cap: int = MAX_LITERAL_AUTO_TESTS,
    preview_limit: int = OVER_CAP_PREVIEW_LIMIT,
) -> str:
    preview = ", ".join(hits[:preview_limit])
    remaining = len(hits) - preview_limit
    suffix = f" (+{remaining} more)" if remaining > 0 else ""
    return (
        f"diff-checks literal-tests: {len(hits)} hit files exceed auto-run cap ({cap}); "
        f"skipping local auto-run. Leading hits: {preview}{suffix}"
    )
