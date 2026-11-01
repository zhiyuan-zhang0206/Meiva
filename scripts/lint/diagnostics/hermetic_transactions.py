# meiva:note
# Lint: hermetic database transactions (no external network, LLM, subprocess, or sleep in tx).
#
# A database transaction pins a connection from the pool and holds row/table locks
# until commit or rollback. Executing external I/O (network requests, LLM calls,
# subprocesses, or sleep) while holding a transaction open causes cascading
# connection pool exhaustion and cluster-wide latency spikes (DDIA §7 & §8).
#
# This lint makes any external I/O directly inside a database transaction block
# (`with write_transaction(...)`, `async with async_write_transaction(...)`,
# `with conn.transaction(...)`, etc.) a commit-time failure.
#
# Remedy (Read-Execute-Write posture):
#   1. Query phase: Open transaction, read required data, and close/commit immediately.
#   2. External I/O phase: Perform HTTP, LLM, or subprocess calls outside the transaction.
#   3. Commit phase: Open a fresh transaction, verify version/preconditions, and persist results.
#
# Exemption:
#   An intentional, tightly bounded in-process mock or fixture opts out via an inline
#   comment: `# hermetic-tx-ok: <reason>` or `# tx-external-io-ok: <reason>`.

from __future__ import annotations

import ast
import re
import sys
from collections.abc import Sequence
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]

sys.path.insert(0, str(_REPO_ROOT))

# meiva:link scripts/docs/scripts.meiva.okf.md
from meizel import lint_common  # noqa: E402
from meizel.checkout import roles_at  # noqa: E402
from meizel.roles import select_packages  # noqa: E402

ROLES = roles_at(_REPO_ROOT)
SCAN_DIRS = select_packages(ROLES, "application", "standalone", "tooling")

_RULE_INPUTS = ("scripts/lint/diagnostics/hermetic_transactions.py",)

_TEST_PATTERNS = (
    re.compile(r"(^|/)tests?/"),
    re.compile(r"(^|/)test_[^/]+\.py$"),
    re.compile(r"_test\.py$"),
)

_TX_FUNCTION_NAMES = frozenset({"write_transaction", "async_write_transaction", "transaction"})

_NETWORK_MODULE_PREFIXES = (
    "httpx.",
    "httpx",
    "requests.",
    "requests",
    "aiohttp.",
    "aiohttp",
    "urllib.request.",
    "urllib.request",
    "urllib.urlopen",
    "socket.",
    "socket",
    "http.client.",
    "http.client",
)

_HTTP_CLIENT_PREFIXES = (
    "http_client.",
    "httpx_client.",
    "aiohttp_session.",
    "requests_session.",
)

_SUBPROCESS_PREFIXES = (
    "subprocess.",
    "subprocess",
    "asyncio.create_subprocess_exec",
    "asyncio.create_subprocess_shell",
    "os.system",
    "os.popen",
    "os.spawn",
)

_SUBPROCESS_BARE_NAMES = frozenset(
    {
        "Popen",
        "create_subprocess_exec",
        "create_subprocess_shell",
    }
)

_LLM_CLIENT_PREFIXES = (
    "openai.chat.",
    "openai.embeddings.",
    "openai.completions.",
    "openai.OpenAI",
    "openai.AsyncOpenAI",
    "anthropic.messages.",
    "anthropic.Anthropic",
    "anthropic.AsyncAnthropic",
)

_LLM_METHOD_SUFFIXES = (
    ".completions.create",
    ".messages.create",
    ".embeddings.create",
)

_LLM_ASYNC_INVOKE_NAMES = frozenset({"ainvoke", "astream", "abatch", "agenerate", "apredict"})
_LLM_SYNC_INVOKE_NAMES = frozenset({"invoke", "stream", "batch", "generate", "predict"})

_LLM_RECEIVER_SUBSTRINGS = (
    "llm",
    "model",
    "client",
    "chain",
    "agent",
    "runnable",
    "chat",
    "prompt",
)

_EXEMPT_COMMENTS = ("# hermetic-tx-ok", "# tx-external-io-ok")


def _is_test_file(rel_path: str) -> bool:
    return any(p.search(rel_path) for p in _TEST_PATTERNS)


def _get_call_chain(node: ast.expr) -> list[str]:
    parts: list[str] = []
    curr: ast.expr = node
    while isinstance(curr, ast.Attribute):
        parts.append(curr.attr)
        curr = curr.value
    if isinstance(curr, ast.Name):
        parts.append(curr.id)
    parts.reverse()
    return parts


def _is_exempt(lineno: int, lines: Sequence[str]) -> bool:
    if 1 <= lineno <= len(lines):
        line_str = lines[lineno - 1]
        return any(comment in line_str for comment in _EXEMPT_COMMENTS)
    return False


def _match_network_call(dotted: str) -> str | None:
    if any(dotted == prefix or dotted.startswith(prefix) for prefix in _NETWORK_MODULE_PREFIXES):
        return f"Network/HTTP call ({dotted})"
    if any(dotted.startswith(prefix) for prefix in _HTTP_CLIENT_PREFIXES):
        return f"HTTP client call ({dotted})"
    return None


def _match_sleep_call(dotted: str, raw_chain: Sequence[str]) -> str | None:
    if dotted in ("time.sleep", "asyncio.sleep") or (
        len(raw_chain) == 1 and raw_chain[0] == "sleep"
    ):
        return f"Sleep call ({dotted})"
    return None


def _match_subprocess_call(dotted: str, raw_chain: Sequence[str]) -> str | None:
    if any(dotted == prefix or dotted.startswith(prefix) for prefix in _SUBPROCESS_PREFIXES) or (
        len(raw_chain) == 1 and raw_chain[0] in _SUBPROCESS_BARE_NAMES
    ):
        return f"Subprocess call ({dotted})"
    return None


def _match_llm_call(func: ast.expr, dotted: str, raw_chain: Sequence[str]) -> str | None:
    if isinstance(func, ast.Attribute) and func.attr in _LLM_ASYNC_INVOKE_NAMES:
        return f"LLM / Runnable async invocation ({dotted})"
    if isinstance(func, ast.Attribute) and func.attr in _LLM_SYNC_INVOKE_NAMES:
        recv = ".".join(raw_chain[:-1]).lower()
        if any(k in recv for k in _LLM_RECEIVER_SUBSTRINGS):
            return f"LLM invocation ({dotted})"
    if any(dotted.startswith(prefix) for prefix in _LLM_CLIENT_PREFIXES):
        return f"LLM library client call ({dotted})"
    if (
        isinstance(func, ast.Attribute)
        and func.attr == "create"
        and any(dotted.endswith(suffix) for suffix in _LLM_METHOD_SUFFIXES)
    ):
        return f"LLM client create call ({dotted})"
    return None


def _format_snippet(node: ast.Call) -> str:
    snippet = ast.unparse(node).replace("\n", " ")
    return snippet[:77] + "..." if len(snippet) > 80 else snippet


class _ImportCollector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.aliases: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            name = alias.asname or alias.name
            self.aliases[name] = alias.name

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        mod = node.module or ""
        for alias in node.names:
            name = alias.asname or alias.name
            target = f"{mod}.{alias.name}" if mod else alias.name
            self.aliases[name] = target


class _HermeticTxVisitor(ast.NodeVisitor):
    def __init__(self, aliases: dict[str, str], lines: Sequence[str]) -> None:
        self.aliases = aliases
        self.lines = lines
        self.errors: list[tuple[int, str]] = []
        self._tx_depth = 0

    def _is_tx_expr(self, expr: ast.expr) -> bool:
        if isinstance(expr, ast.Call):
            func = expr.func
            if isinstance(func, ast.Name):
                return func.id in _TX_FUNCTION_NAMES
            if isinstance(func, ast.Attribute):
                return func.attr in _TX_FUNCTION_NAMES
        elif isinstance(expr, ast.Attribute):
            return expr.attr in ("write_transaction", "async_write_transaction")
        elif isinstance(expr, ast.Name):
            return expr.id in ("write_transaction", "async_write_transaction")
        return False

    def visit_With(self, node: ast.With) -> None:
        is_tx = any(self._is_tx_expr(item.context_expr) for item in node.items)
        if is_tx:
            self._tx_depth += 1
            for stmt in node.body:
                self.visit(stmt)
            self._tx_depth -= 1
        else:
            self.generic_visit(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        is_tx = any(self._is_tx_expr(item.context_expr) for item in node.items)
        if is_tx:
            self._tx_depth += 1
            for stmt in node.body:
                self.visit(stmt)
            self._tx_depth -= 1
        else:
            self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if self._tx_depth > 0:
            self._check_tx_call(node)
        self.generic_visit(node)

    def _resolve_dotted(self, node: ast.expr) -> str:
        chain = _get_call_chain(node)
        if not chain:
            return ""
        base = chain[0]
        if base in self.aliases:
            resolved_base = self.aliases[base]
            return ".".join([resolved_base, *chain[1:]])
        return ".".join(chain)

    def _check_tx_call(self, node: ast.Call) -> None:
        if _is_exempt(node.lineno, self.lines):
            return

        func = node.func
        dotted = self._resolve_dotted(func)
        raw_chain = _get_call_chain(func)

        reason = (
            _match_network_call(dotted)
            or _match_sleep_call(dotted, raw_chain)
            or _match_subprocess_call(dotted, raw_chain)
            or _match_llm_call(func, dotted, raw_chain)
        )

        if reason is not None:
            self.errors.append((node.lineno, f"{reason}: {_format_snippet(node)}"))


def scan_source(text: str) -> list[tuple[int, str]]:
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return []
    collector = _ImportCollector()
    collector.visit(tree)
    lines = text.splitlines()
    visitor = _HermeticTxVisitor(collector.aliases, lines)
    visitor.visit(tree)
    return visitor.errors


def scan_file(path: Path) -> list[tuple[int, str]]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return []
    return scan_source(text)


def _iter_py_files(roots: Sequence[Path], repo_root: Path) -> list[Path]:
    return lint_common.python_files(list(roots), repo_root)


def _resolve_targets(
    argv: list[str],
    only: list[str] | None,
    repo_root: Path,
    dirs: Sequence[str],
) -> tuple[list[Path], frozenset[str] | None] | int:
    if argv:
        missing = [arg for arg in argv if not Path(arg).exists()]
        if missing:
            print(f"error: target path(s) not found: {', '.join(missing)}", file=sys.stderr)
            return 1
        targets = [Path(a).resolve() for a in argv]
    else:
        targets = lint_common.scan_roots(repo_root, dirs)
    scope = lint_common.changed_scope(only, repo_root, inputs=_RULE_INPUTS)
    return targets, scope


def _scan_paths(files: Sequence[Path], repo_root: Path) -> tuple[int, int]:
    total_violations = 0
    files_with_violations = 0
    for path in files:
        rel = (
            path.relative_to(repo_root).as_posix()
            if path.is_relative_to(repo_root)
            else path.as_posix()
        )
        if _is_test_file(rel):
            continue
        errors = scan_file(path)
        if errors:
            files_with_violations += 1
            for lineno, reason in errors:
                total_violations += 1
                print(f"{rel}:{lineno}: {reason}")
    return total_violations, files_with_violations


def _print_ddia_diagnostic(total_violations: int, files_with_violations: int) -> None:
    print(
        f"\n{total_violations} non-hermetic transaction violation(s) across "
        f"{files_with_violations} file(s).\n\n"
        "DDIA Violation (Cascading Connection Pool Exhaustion):\n"
        "Holding an open database transaction while executing external I/O\n"
        "(network HTTP, LLM inference, subprocesses, sleep) pins a connection\n"
        "and locks idle in transaction (DDIA §7 & §8).\n"
        "Under concurrency or latency spikes, connections remain locked, exhausting\n"
        "the connection pool and triggering cascading timeouts cluster-wide.\n\n"
        "Remedy (Read-Execute-Write posture):\n"
        "  1. Query phase: Open transaction, fetch data, and close transaction immediately.\n"
        "  2. External I/O phase: Run HTTP, LLM, or subprocess outside any transaction.\n"
        "  3. Commit phase: Open fresh transaction, verify preconditions, and save results.",
        file=sys.stderr,
    )


def main(
    argv: list[str] | None = None,
    *,
    repo_root: Path = _REPO_ROOT,
    scan_dirs: Sequence[str] | None = None,
) -> int:
    argv = argv if argv is not None else sys.argv[1:]
    raw_args, only = lint_common.split_only(argv)
    dirs = SCAN_DIRS if scan_dirs is None else scan_dirs
    parsed = _resolve_targets(raw_args, only, repo_root, dirs)
    if isinstance(parsed, int):
        return parsed

    targets, scope = parsed
    files_to_scan = sorted(
        lint_common.restrict(_iter_py_files(targets, repo_root), scope, repo_root)
    )

    total_violations, files_with_violations = _scan_paths(files_to_scan, repo_root)
    if total_violations:
        _print_ddia_diagnostic(total_violations, files_with_violations)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
