# meiva:note
# The `argv` roots of the command-line entries keep one shape. A script's `main` takes its argument
# list as a required parameter and the file that holds the `__main__` guard reads `sys.argv[1:]`
# once and hands it down. A `main(argv=None)` that falls back to the process command line reads the
# environment from inside the callee, which the root declaration of the guard file cannot account
# for; handing that `None` to `parser.parse_args(argv)` is the same fallback, because argparse then
# reads `sys.argv[1:]` itself.

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[4]

# meiva:note
# (file holding the guard, file defining the `main` the guard calls; None when it is the same file)
_ENTRIES = [
    ("base/cluster/frontend_build.py", None),
    ("base/deploy/git/check_git_hooks.py", None),
    (
        "meiva_builtins/skills/integrations/web-sources/generic/scripts/feed.py",
        "meiva_builtins/skills/integrations/web-sources/scripts/webscrape.py",
    ),
    ("meiva_builtins/skills/integrations/web-sources/rss/scripts/feed.py", None),
    ("meiva_builtins/skills/integrations/web-sources/scripts/webscrape.py", None),
    ("meiva_builtins/skills/practice/meiva-deep-research/scripts/audit_research.py", None),
    (
        "meizel/__main__.py",
        "meizel/entry.py",
    ),
    ("meizel/language/no_script_sibling_imports.py", None),
    ("meizel/owners/clock_lattice_cli.py", None),
    ("meizel/owners/timing_judgement_cli.py", None),
    (
        "ops/private_files/__main__.py",
        "ops/private_files/__init__.py",
    ),
    ("scripts/audit/branch_protection.py", None),
    ("scripts/audit/call_sites.py", None),
    ("scripts/audit/ensure_plugins_loaded_context_census.py", None),
    ("scripts/audit/impact_census.py", None),
    ("scripts/audit/schema_drift.py", None),
    ("scripts/audit/sdk_usage.py", None),
    ("scripts/audit/where_used.py", None),
    (
        "scripts/ci/cli.py",
        "scripts/ci/pull_requests/commands.py",
    ),
    ("scripts/ci/coverage_gates.py", None),
    ("scripts/ci/e2e_scope.py", None),
    ("scripts/ci/host_marks_census.py", None),
    ("scripts/ci/pull_requests/accounting.py", None),
    ("scripts/ci/pull_requests/pr_flow_export.py", None),
    ("scripts/ci/pull_requests/pyright_scope.py", None),
    ("scripts/ci/pull_requests/runs_export.py", None),
    ("scripts/ci/shard_counts.py", None),
    ("scripts/ci/test_selector.py", None),
    ("scripts/codegen/build_android_release.py", None),
    ("scripts/codegen/build_app_update_manifest.py", None),
    ("scripts/codegen/gen_pyright_test_environments.py", None),
    (
        "scripts/content_lint/code_comments/__main__.py",
        "scripts/content_lint/code_comments/cli.py",
    ),
    ("scripts/content_lint/lint_audit_record.py", None),
    ("scripts/content_lint/lint_core_content_manifests.py", None),
    ("scripts/content_lint/lint_meiva_okf.py", None),
    ("scripts/content_lint/lint_no_cjk.py", None),
    ("scripts/content_lint/lint_no_tailnet.py", None),
    ("scripts/data_plane_ops/rotate_cluster_secret.py", None),
    ("scripts/data_plane_ops/rotate_data_plane_secrets.py", None),
    ("scripts/hooks/diff_checks.py", None),
    ("scripts/host_ops/guard_editable_venv.py", None),
    ("scripts/host_ops/host_memory_sample.py", None),
    ("scripts/host_ops/onboarding/harness_onboarding.py", None),
    ("scripts/host_ops/release/base.py", None),
    ("scripts/host_ops/release/bisect_train.py", None),
    ("scripts/host_ops/release/certify.py", None),
    ("scripts/host_ops/release/nightly.py", None),
    ("scripts/host_ops/release/promote.py", None),
    ("scripts/host_ops/release/tag.py", None),
    ("scripts/host_ops/run_with_token.py", None),
    ("scripts/host_ops/tcc/request_grants_evidence.py", None),
    ("scripts/host_ops/update_schedule_script.py", None),
    ("scripts/lint/async_no_sync_blocking.py", None),
    ("scripts/lint/code_structure.py", None),
    ("scripts/lint/diagnostics/diff_hints/hints.py", None),
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
    ("scripts/lint/policy_removal.py", None),
    ("scripts/lint/pool_keepalives.py", None),
    ("scripts/lint/termination_source.py", None),
    ("scripts/model_registry/check_model_updates.py", None),
    ("scripts/model_registry/check_plugin_price_drift.py", None),
    ("scripts/model_registry/update_model_pricing.py", None),
    ("scripts/provision/apt_lock.py", None),
    (
        "scripts/provision/check_git_hooks.py",
        "base/deploy/git/check_git_hooks.py",
    ),
    ("scripts/structure/cochange.py", None),
    ("scripts/structure/platform_lane_coverage.py", None),
    ("scripts/structure/skip_sites.py", None),
    ("scripts/structure/tests_location.py", None),
    ("scripts/verify/understanding_replay.py", None),
    ("services/agent_runner/agent_ops/daemon.py", None),
    ("services/backup/restore_drill.py", None),
    ("services/derived/labeler/daemon.py", None),
    ("services/redis_bridge/relay.py", None),
    (
        "services/supervision/meiva_root/__main__.py",
        "services/supervision/meiva_root/daemon.py",
    ),
    ("services/upkeep/events_maintenance/rollup.py", None),
    ("ui/app/android/apply_overlay.py", None),
]

# meiva:note
# Entries whose function is not called `main`: (file holding the guard, file defining it, name).
_NAMED = [("services/backup/dump.py", None, "run_offsite_publisher")]

_CASES = [(guard, definer, "main") for guard, definer in _ENTRIES] + _NAMED

# meiva:note
# Parsers that a `main` hands its argument list to; they too take it as a required parameter, so
# that `None` cannot reach argparse and make it read `sys.argv` there.
_PARSERS = [
    ("scripts/hooks/diff_checks.py", "parse_args"),
    ("services/redis_bridge/relay.py", "_parse_args"),
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


def _hands_the_tail(call: ast.Call) -> bool:
    positional = bool(call.args) and _is_argv_tail(call.args[0])
    keyword = any(kw.arg == "argv" and _is_argv_tail(kw.value) for kw in call.keywords)
    return positional or keyword


def _functions(relative: str, name: str) -> list[ast.FunctionDef]:
    return [
        node
        for node in _tree(relative).body
        if isinstance(node, ast.FunctionDef) and node.name == name
    ]


def _requires_argv(function: ast.FunctionDef) -> bool:
    arguments = function.args
    if [a.arg for a in arguments.args][:1] == ["argv"]:
        return len(arguments.defaults) == 0
    keyword_only = dict(
        zip((a.arg for a in arguments.kwonlyargs), arguments.kw_defaults, strict=True)
    )
    return "argv" in keyword_only and keyword_only["argv"] is None


@pytest.mark.parametrize(("guard_file", "definer", "name"), _CASES)
def test_the_guard_hands_the_command_line_to_main(
    guard_file: str, definer: str | None, name: str
) -> None:
    handed = [
        call
        for call in _guard_calls(_tree(guard_file))
        if isinstance(call.func, ast.Name) and call.func.id == name and _hands_the_tail(call)
    ]
    assert handed, f"{guard_file}: no `{name}(sys.argv[1:], ...)` call under the __main__ guard"


@pytest.mark.parametrize(("guard_file", "definer", "name"), _CASES)
def test_main_requires_its_argument_list(guard_file: str, definer: str | None, name: str) -> None:
    defining = definer or guard_file
    functions = _functions(defining, name)
    assert len(functions) == 1, f"{defining}: expected one module-level `{name}`"
    assert _requires_argv(functions[0]), f"{defining}: `{name}` must take a required `argv`"


@pytest.mark.parametrize(("file", "name"), _PARSERS)
def test_a_parser_of_the_argument_list_requires_it(file: str, name: str) -> None:
    functions = _functions(file, name)
    assert len(functions) == 1, f"{file}: expected one module-level `{name}`"
    assert _requires_argv(functions[0]), f"{file}: `{name}` must take a required `argv`"


# meiva:note
# The console script `meiva = cli.main:entry` is the process entry of the CLI, not a `__main__`
# guard: `entry()` reads `sys.argv[1:]` once and hands it to `main`, and `main` and `run_command`
# take it as a required parameter and never read `sys.argv` themselves.
def test_the_cli_entry_reads_the_command_line_once() -> None:
    entries = _functions("cli/main.py", "entry")
    assert len(entries) == 1
    handed = [
        call
        for call in ast.walk(entries[0])
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Name)
        and call.func.id == "main"
        and _hands_the_tail(call)
    ]
    assert handed, "cli/main.py: `entry()` must call `main(sys.argv[1:])`"


@pytest.mark.parametrize("name", ["main", "run_command"])
def test_the_cli_dispatch_takes_its_argument_list_and_does_not_read_it(name: str) -> None:
    functions = _functions("cli/main.py", name)
    assert len(functions) == 1 and _requires_argv(functions[0]), f"`{name}` needs a required argv"
    reads = [
        node
        for node in ast.walk(functions[0])
        if isinstance(node, ast.Attribute) and ast.unparse(node) == "sys.argv"
    ]
    assert not reads, f"cli/main.py: `{name}` must not read sys.argv"


# meiva:note
# The core-content manifest gate expands the `~` of its `--audit-dir` against a home it is given:
# `main` takes `home` as a required keyword and the guard reads `Path.home()` once.
def test_the_core_content_manifest_gate_is_given_its_home() -> None:
    relative = "scripts/content_lint/lint_core_content_manifests.py"
    functions = _functions(relative, "main")
    assert len(functions) == 1
    keyword_only = dict(
        zip(
            (a.arg for a in functions[0].args.kwonlyargs),
            functions[0].args.kw_defaults,
            strict=True,
        )
    )
    assert "home" in keyword_only and keyword_only["home"] is None, "`home` must be required"
    handed = [
        call
        for call in _guard_calls(_tree(relative))
        if isinstance(call.func, ast.Name)
        and call.func.id == "main"
        and any(kw.arg == "home" and ast.unparse(kw.value) == "Path.home()" for kw in call.keywords)
    ]
    assert handed, f"{relative}: the guard must call `main(..., home=Path.home())`"
