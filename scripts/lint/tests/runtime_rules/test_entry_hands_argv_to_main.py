# meiva:note
# The `argv` roots of the command-line entries keep one shape. A script's `main` takes its argument
# list as a required parameter and the file that holds the `__main__` guard reads `sys.argv[1:]`
# once and hands it down. A `main(argv=None)` that falls back to the process command line reads the
# environment from inside the callee, which the root declaration of the guard file cannot account
# for.

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]

# meiva:note
# (file holding the guard, file defining the `main` the guard calls; None when it is the same file)
_ENTRIES = [
    ("meizel/language/no_script_sibling_imports.py", None),
    ("meizel/owners/clock_lattice_cli.py", None),
    ("meizel/owners/timing_judgement_cli.py", None),
    ("scripts/audit/ensure_plugins_loaded_context_census.py", None),
    ("scripts/content_lint/lint_audit_record.py", None),
    ("scripts/content_lint/lint_core_content_manifests.py", None),
    ("scripts/content_lint/lint_no_cjk.py", None),
    ("scripts/content_lint/lint_no_tailnet.py", None),
    ("scripts/ci/coverage_gates.py", None),
    ("scripts/host_ops/guard_editable_venv.py", None),
    ("scripts/host_ops/run_with_token.py", None),
    ("scripts/lint/async_no_sync_blocking.py", None),
    ("scripts/lint/code_structure.py", None),
    ("scripts/lint/diagnostics/hermetic_transactions.py", None),
    ("scripts/lint/diagnostics/logger_add_diagnose.py", None),
    ("scripts/lint/diagnostics/loguru_format.py", None),
    ("scripts/lint/diagnostics/no_ambient_fallback.py", None),
    ("scripts/lint/diagnostics/no_emoji.py", None),
    ("scripts/lint/diagnostics/no_silent_failures.py", None),
    (
        "scripts/lint/diagnostics/time_bomb.py",
        "scripts/lint/diagnostics/time_bomb_rule/entry.py",
    ),
    ("scripts/lint/diagnostics/zombie_pyright_ignores.py", None),
    ("scripts/lint/fixture_scope.py", None),
    ("scripts/lint/host_platform_marks.py", None),
    ("scripts/lint/meiva_root_scope.py", None),
    ("scripts/lint/no_os_environ.py", None),
    ("scripts/lint/pool_keepalives.py", None),
    ("scripts/lint/termination_source.py", None),
    ("scripts/structure/cochange.py", None),
    ("scripts/structure/platform_lane_coverage.py", None),
    ("scripts/structure/skip_sites.py", None),
    ("scripts/structure/tests_location.py", None),
    ("services/supervision/meiva_root/__main__.py", "services/supervision/meiva_root/daemon.py"),
    (
        "meiva_builtins/skills/integrations/web-sources/generic/scripts/feed.py",
        "meiva_builtins/skills/integrations/web-sources/scripts/webscrape.py",
    ),
    ("meiva_builtins/skills/integrations/web-sources/rss/scripts/feed.py", None),
    ("meiva_builtins/skills/integrations/web-sources/scripts/webscrape.py", None),
]


def _tree(relative: str) -> ast.Module:
    return ast.parse((_REPO_ROOT / relative).read_text(encoding="utf-8"))


def _is_main_guard(node: ast.stmt) -> bool:
    if not isinstance(node, ast.If) or not isinstance(node.test, ast.Compare):
        return False
    test = node.test
    return (
        isinstance(test.left, ast.Name)
        and test.left.id == "__name__"
        and len(test.comparators) == 1
        and isinstance(test.comparators[0], ast.Constant)
        and test.comparators[0].value == "__main__"
    )


def _is_argv_tail(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Subscript)
        and ast.unparse(node.value) == "sys.argv"
        and isinstance(node.slice, ast.Slice)
        and isinstance(node.slice.lower, ast.Constant)
        and node.slice.lower.value == 1
        and node.slice.upper is None
    )


def _guard_calls(tree: ast.Module) -> list[ast.Call]:
    return [
        call
        for node in tree.body
        if _is_main_guard(node)
        for call in ast.walk(node)
        if isinstance(call, ast.Call)
    ]


@pytest.mark.parametrize(("guard_file", "definer"), _ENTRIES)
def test_the_guard_hands_the_command_line_to_main(guard_file: str, definer: str | None) -> None:
    handed = [
        call
        for call in _guard_calls(_tree(guard_file))
        if isinstance(call.func, ast.Name)
        and call.func.id == "main"
        and call.args
        and _is_argv_tail(call.args[0])
    ]
    assert handed, f"{guard_file}: no `main(sys.argv[1:], ...)` call under the __main__ guard"


@pytest.mark.parametrize(("guard_file", "definer"), _ENTRIES)
def test_main_requires_its_argument_list(guard_file: str, definer: str | None) -> None:
    defining = definer or guard_file
    mains = [
        node
        for node in _tree(defining).body
        if isinstance(node, ast.FunctionDef) and node.name == "main"
    ]
    assert len(mains) == 1, f"{defining}: expected one module-level `main`"
    arguments = mains[0].args
    assert [a.arg for a in arguments.args][:1] == ["argv"], f"{defining}: `main` must take argv"
    assert len(arguments.defaults) == 0, f"{defining}: `argv` must not have a default"
