"""Runtime impact regressions independent of nearest-package ownership buckets."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts.ci import test_impact, test_selector


def _write(root: Path, name: str, text: str = "") -> None:
    file = root / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(text)


def _repo(root: Path) -> Path:
    _write(
        root,
        "pyproject.toml",
        '[tool.pytest.ini_options]\ntestpaths = ["tests", "base/**/tests"]\n',
    )
    return root


def _assert_selected(
    root: Path, changed: str, expected: str, *, base_ref: str | None = None
) -> None:
    result = test_selector.select_tests([changed], repo_root=root, base_ref=base_ref)
    assert result.decision == "SELECTED", result.as_json()
    assert expected in result.tests


def test_transitive_relative_helpers_and_imported_tests_are_runtime_edges(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "tests/support/inner.py", "from base import leaf\n")
    _write(root, "tests/support/outer.py", "from . import inner\n")
    _write(root, "base/tests/test_shared.py", "from tests.support import outer\n")
    _write(root, "tests/consumer/test_scenario.py", "from base.tests import test_shared\n")
    reverse = test_selector.build_import_reverse_map(root)
    assert reverse["base/leaf.py"] == {
        "base/tests/test_shared.py",
        "tests/consumer/test_scenario.py",
    }
    _assert_selected(root, "tests/support/inner.py", "tests/consumer/test_scenario.py")
    _assert_selected(root, "base/tests/test_shared.py", "tests/consumer/test_scenario.py")


def test_shared_dependency_cycle_propagates_every_consumer(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "base/left.py", "from base import right\n")
    _write(root, "base/right.py", "from base import left, leaf\n")
    _write(root, "tests/test_left.py", "from base import left\n")
    _write(root, "tests/test_right.py", "from base import right\n")
    _write(root, "tests/test_consumer.py", "from tests import test_left\n")
    _write(root, "base/unloaded.py", "import importlib\nimportlib.import_module(target)\n")

    reverse = test_selector.build_import_reverse_map(root)

    consumers = {"tests/test_left.py", "tests/test_right.py", "tests/test_consumer.py"}
    for dependency in ("base/left.py", "base/right.py", "base/leaf.py"):
        assert reverse[dependency] == consumers
    assert reverse["tests/test_left.py"] == {"tests/test_left.py", "tests/test_consumer.py"}
    assert "base/unloaded.py" not in reverse
    _assert_selected(root, "base/leaf.py", "tests/test_consumer.py")


def test_empty_consumer_universe_has_no_reachable_unknowns(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/orphan.py", "import importlib\nimportlib.import_module(target)\n")

    impact = test_impact.build_impact(root, frozenset())

    assert impact.tests_by_input == {}
    assert impact.unknown == ()


def test_path_scoped_fixture_closure_reaches_tests_outside_its_owner(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "agent/tests/agent_tests.py", "from base import leaf\n")
    _write(root, "base/consumer/tests/test_scope.py", "def test_scope(): pass\n")
    _write(root, "base/consumer/tests/path_scopes.toml", '"agent.tests.agent_tests" = ["."]\n')
    _assert_selected(root, "agent/tests/agent_tests.py", "base/consumer/tests/test_scope.py")
    _assert_selected(root, "base/leaf.py", "base/consumer/tests/test_scope.py")


def test_collecting_a_packaged_test_executes_its_package_initializers(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "base/consumer/__init__.py", "from base import leaf\n")
    _write(root, "base/consumer/tests/test_scope.py", "def test_scope(): pass\n")
    _assert_selected(root, "base/leaf.py", "base/consumer/tests/test_scope.py")


def test_repository_directory_reads_reach_changed_descendants(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(
        root,
        "tests/consumer/test_scan.py",
        """from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
def test_scan():
    assert list(ROOT.rglob("*.py"))
""",
    )
    _assert_selected(root, "base/leaf.py", "tests/consumer/test_scan.py")


def test_the_root_conftest_and_its_plugins_are_not_a_per_test_input(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "tests/plugin.py", "from base import leaf\n")
    _write(root, "conftest.py", 'pytest_plugins = ["tests.plugin"]\n')
    _write(root, "tests/test_lint_scan.py", "def test_scan(): pass\n")
    _write(root, "base/other/tests/test_other.py", "def test_other(): pass\n")

    result = test_selector.select_tests(["base/leaf.py"], repo_root=root)

    # meiva:note
    # Every test loads the root conftest and its plugin, so the plugin's production closure used to
    # reach all tests. Changing the root conftest or the plugin is a global change (FULL), so the
    # closure is not also charged to each test; the harness import contract covers what they import.
    assert test_selector.build_import_reverse_map(root).get("base/leaf.py", set()) == set()
    assert (result.decision, result.tests) == ("SELECTED", ("tests/test_lint_scan.py",))
    for global_path in ("conftest.py", "tests/plugin.py"):
        full = test_selector.select_tests([global_path], repo_root=root)
        assert (full.decision, full.reason) == ("FULL", f"global-path:{global_path}")


def test_a_nested_conftest_is_still_a_runtime_dependency_of_its_tests(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "base/consumer/tests/conftest.py", "from base import leaf\n")
    _write(root, "base/consumer/tests/test_scope.py", "def test_scope(): pass\n")
    _assert_selected(root, "base/leaf.py", "base/consumer/tests/test_scope.py")


@pytest.mark.parametrize(
    ("source", "changed"),
    [
        (
            'import subprocess\nsubprocess.run(["python", "-m", "services.agent_runner.pty_sessions.daemon"])\n',
            "services/agent_runner/pty_sessions/daemon.py",
        ),
        (
            'import subprocess\nsubprocess.run(["python", "-c", "from base import leaf"])\n',
            "base/leaf.py",
        ),
        (
            'from pathlib import Path\nROOT = Path(__file__).resolve().parents[2]\n(ROOT / "base/data/prompt.txt").read_text()\n',
            "base/data/prompt.txt",
        ),
    ],
)
def test_literal_process_and_resource_boundaries_select_the_consumer(
    tmp_path: Path,
    source: str,
    changed: str,
) -> None:
    root = _repo(tmp_path)
    _write(root, changed)
    _write(root, "tests/security/test_argv.py", source)
    _assert_selected(root, changed, "tests/security/test_argv.py")


def test_finite_parametrized_plugin_imports_select_the_registry_consumer(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "meiva_builtins/plugins/meiva_memory/metrics.py")
    _write(
        root,
        "tests/registry/test_metrics.py",
        """import importlib
import pytest
@pytest.mark.parametrize("plugin", ["meiva_memory"])
def test_metrics(plugin):
    module_name = f"meiva_builtins.plugins.{plugin}.metrics"
    importlib.import_module(module_name)
""",
    )
    _assert_selected(
        root, "meiva_builtins/plugins/meiva_memory/metrics.py", "tests/registry/test_metrics.py"
    )


def test_opaque_import_consumer_always_runs_visibly_and_syntax_errors_fail(
    tmp_path: Path,
) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(
        root,
        "tests/consumer/test_dynamic.py",
        "import importlib\nimportlib.import_module(module_name)\n",
    )
    result = test_selector.select_tests(["base/leaf.py"], repo_root=root)
    # The opaque import may load the changed module, so its test joins every subset.
    assert result.decision == "SELECTED", result.as_json()
    assert "tests/consumer/test_dynamic.py" in result.tests
    assert any("tests/consumer/test_dynamic.py:2:" in item for item in result.diagnostics)
    _write(root, "tests/consumer/test_dynamic.py", "def syntax error\n")
    with pytest.raises(SyntaxError):
        test_selector.select_tests(["base/leaf.py"], repo_root=root)


def test_unknown_in_an_unloaded_helper_does_not_force_full(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "tests/support/orphan.py", "import importlib\nimportlib.import_module(name)\n")
    _write(root, "tests/consumer/test_leaf.py", "from base import leaf\n")
    _assert_selected(root, "base/leaf.py", "tests/consumer/test_leaf.py")


def _git(root: Path, *args: str) -> str:
    return subprocess.run(  # noqa: S603 - fixed Git commands against a test-owned checkout
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


@pytest.mark.parametrize("deleted", [False, True])
def test_base_facts_preserve_removed_dependency_impact(tmp_path: Path, deleted: bool) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "tests/support/helper.py", "from base import leaf\n")
    _write(root, "tests/consumer/test_old.py", "from tests.support import helper\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")
    _write(root, "tests/support/helper.py")
    if deleted:
        (root / "base/leaf.py").unlink()
    _assert_selected(root, "base/leaf.py", "tests/consumer/test_old.py", base_ref=base)


def test_base_archive_cannot_silently_omit_dependency_inputs(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, ".gitattributes", "base/leaf.py export-ignore\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    with pytest.raises(RuntimeError, match=r"Base archive omitted tracked inputs.*base/leaf\.py"):
        test_selector.select_tests(["base/leaf.py"], repo_root=root, base_ref="HEAD")


@pytest.mark.parametrize(
    "legacy",
    [
        '[ambient-state]\ndb-modules = ["base.db"]\ndb-primitives = ["connect"]\n'
        'db-handles = ["base.db:Database"]\nbus-handles = ["base.bus:EventBus"]\n'
        'endpoint-handles = ["base.endpoints:ServiceEndpoints"]\n',
        "[ambient-state]\nunknown = []\n",
        "[ambient-state]\ndb-handles = [3]\n",
    ],
)
def test_base_policy_uses_the_owner_snapshot_decoder(tmp_path: Path, legacy: str) -> None:
    from meizel.graph.model import MeizelError

    root = _repo(tmp_path)
    _write(root, "base/leaf.py")
    _write(root, "tests/consumer/test_old.py", "from base import leaf\n")
    _write(root, "meizel.policy.toml", legacy)
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    _write(root, "tests/consumer/test_old.py", "def test_old(): pass\n")
    _write(root, "meizel.policy.toml")
    if "unknown" in legacy or "[3]" in legacy:
        with pytest.raises(MeizelError, match="cannot read the manifests"):
            test_selector.select_tests(["base/leaf.py"], repo_root=root, base_ref="HEAD")
    else:
        _assert_selected(root, "base/leaf.py", "tests/consumer/test_old.py", base_ref="HEAD")
    assert (root / "meizel.policy.toml").read_text() == ""


def test_interface_early_cutoff_isolates_strict_module_internal_edits(tmp_path: Path) -> None:
    root = _repo(tmp_path)
    _write(root, "base/lm/meizel.toml", 'purity = "strict"\n')
    _write(
        root,
        "base/lm/retry.py",
        "def compute_backoff(attempt: int, *, backoff_ms: int = 100) -> int:\n"
        "    return backoff_ms * attempt\n",
    )
    _write(
        root,
        "base/lm/tests/test_lm.py",
        "from base.lm.retry import compute_backoff\n\ndef test_lm():\n    assert compute_backoff(2) == 200\n",
    )
    _write(
        root,
        "tests/consumer/test_cli.py",
        "from base.lm.retry import compute_backoff\n\ndef test_cli():\n    assert compute_backoff(1) == 100\n",
    )
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "-qm", "base")
    base = _git(root, "rev-parse", "HEAD")

    _write(
        root,
        "base/lm/retry.py",
        "def _clamp(value: int) -> int:\n"
        "    return max(0, value)\n\n"
        "def compute_backoff(attempt: int, *, backoff_ms: int = 100) -> int:\n"
        "    return _clamp(backoff_ms * attempt)\n",
    )
    internal = test_selector.select_tests(["base/lm/retry.py"], repo_root=root, base_ref=base)
    assert internal.decision == "SELECTED"
    assert internal.tests == ("base/lm/tests/test_lm.py",)

    _write(
        root,
        "base/lm/retry.py",
        "def compute_backoff(attempt: int, *, backoff_s: float = 0.1) -> float:\n"
        "    return backoff_s * attempt\n",
    )
    signature_changed = test_selector.select_tests(
        ["base/lm/retry.py"], repo_root=root, base_ref=base
    )
    assert signature_changed.decision == "SELECTED"
    assert signature_changed.tests == ("base/lm/tests/test_lm.py", "tests/consumer/test_cli.py")


_BATCH_BODY = """This pull request is based on the [main branch](https://github.com/o/r/tree/main) at [SHA 02af8a4dc690](https://github.com/o/r/commit/02af8a4dc690).

See more details about each PR in the batch here:
* [PR 7603](https://app.trunk.io/ava/merge-queue/queue/7603)
* [PR 7610](https://app.trunk.io/ava/merge-queue/queue/7610)

## Pull Requests Being Tested

This pull request is testing a batch with the changes from pull requests [7603](https://www.github.com/o/r/pull/7603) and [7610](https://www.github.com/o/r/pull/7610) - [batching documentation](https://docs.trunk.io/merge-queue/batching).
"""


def test_the_members_are_the_links_under_the_queue_heading() -> None:
    assert test_impact.member_numbers(_BATCH_BODY) == [7603, 7610]


def test_a_link_outside_the_heading_is_not_a_member() -> None:
    body = "See the change in [7603](https://x/pull/7603).\n\n## Pull Requests Being Tested\n\n"
    assert test_impact.member_numbers(body) == []


def test_a_body_without_the_heading_names_nobody() -> None:
    assert test_impact.member_numbers("An ordinary pull request [7603](https://x/pull/7603).") == []


def test_the_event_payload_carries_the_members_without_a_token(tmp_path: Path) -> None:
    payload = tmp_path / "event.json"
    payload.write_text(json.dumps({"pull_request": {"body": _BATCH_BODY, "number": 7659}}))
    assert test_impact.members_from_event(payload) == (7603, 7610)


def test_an_unreadable_payload_answers_none_rather_than_no_members(tmp_path: Path) -> None:
    # meiva:note
    # None is "this run cannot tell", never "the batch has no members": the selector keeps the full
    # suite on None, and would otherwise select from a list it never read.
    assert test_impact.members_from_event(tmp_path / "absent.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    assert test_impact.members_from_event(broken) is None
    no_request = tmp_path / "push.json"
    no_request.write_text(json.dumps({"ref": "refs/heads/main"}))
    assert test_impact.members_from_event(no_request) is None
    no_members = tmp_path / "plain.json"
    no_members.write_text(json.dumps({"pull_request": {"body": "An ordinary pull request."}}))
    assert test_impact.members_from_event(no_members) is None
