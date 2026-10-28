# meiva:note
# `meizel/state/no_os_environ_plan.py` unit tests: which paths the rule judges as test files, which
# lines of a file set an environment variable the settings singleton models, how a path is labelled
# and what a finding and the summary say. Asserted by value; the rule end to end against a temporary
# tree is `test_lint_no_os_environ.py`.
#
# The bodies below spell the variable through `_NAME` because this file is a test file itself: a
# literal `monkeypatch.setenv("<managed name>"` here would be a finding of the very rule it tests.
# meiva:link meizel/docs/meizel.meiva.okf.md

from __future__ import annotations

from pathlib import Path

import pytest

from meizel.state import no_os_environ_plan as plan

_ROOT = Path("/repo")
_NAME = "MEIVA_DB_URL"
_QUOTES = '"' * 3


@pytest.mark.parametrize(
    ("rel_path", "is_test"),
    [
        ("tests/mod.py", True),
        ("base/tests/mod.py", True),
        ("base/pkg/test_mod.py", True),
        ("base/pkg/mod_test.py", True),
        ("base/pkg/mod.py", False),
        ("contest/mod.py", False),
        ("base/testing/mod.py", False),
        ("base/testsupport/mod.py", False),
        ("base/pkg/testhelpers.py", False),
    ],
)
def test_only_test_paths_are_test_files(rel_path: str, is_test: bool) -> None:
    cli = frozenset({"cli"})
    assert plan.in_default_scope(_ROOT / "cli" / rel_path, _ROOT, test_only_dirs=cli) is is_test


def test_the_rule_judges_a_test_file_it_has_not_exempted() -> None:
    judged = _ROOT / "base/pkg/test_mod.py"
    assert plan.judges_file(judged, _ROOT, allowed_files=frozenset()) is True
    assert (
        plan.judges_file(judged, _ROOT, allowed_files=frozenset({"base/pkg/test_mod.py"})) is False
    )
    assert plan.judges_file(_ROOT / "base/pkg/mod.py", _ROOT, allowed_files=frozenset()) is False


def test_a_skipped_directory_is_scanned_only_for_its_test_files() -> None:
    dirs = frozenset({"cli"})
    assert plan.in_default_scope(_ROOT / "cli/pkg/test_mod.py", _ROOT, test_only_dirs=dirs) is True
    assert plan.in_default_scope(_ROOT / "cli/pkg/mod.py", _ROOT, test_only_dirs=dirs) is False
    assert plan.in_default_scope(_ROOT / "base/mod.py", _ROOT, test_only_dirs=dirs) is True


def test_the_default_scan_reads_tooling_tests_and_the_test_only_directories() -> None:
    assert plan.scan_targets(
        ["base", "agent", "cli"], ["scripts", "meizel"], frozenset({"cli", "ops"})
    ) == ("base", "agent", "scripts", "meizel", "tests", "cli", "ops")


def test_a_path_outside_the_repository_is_not_narrowed() -> None:
    dirs = frozenset({"cli"})
    assert plan.in_default_scope(Path("/elsewhere/cli/mod.py"), _ROOT, test_only_dirs=dirs) is True


def test_a_label_is_relative_inside_the_repository_and_absolute_outside() -> None:
    kind = f"setenv-managed:{_NAME}"
    assert plan.finding(_ROOT / "base/mod.py", _ROOT, 3, kind).startswith("base/mod.py:3: ")
    assert plan.finding(Path("/elsewhere/mod.py"), _ROOT, 3, kind).startswith(
        "/elsewhere/mod.py:3: "
    )


_MANAGED = frozenset({_NAME, "MEIVA_TOKEN"})


def _violations(text: str) -> list[tuple[int, str, str]]:
    return plan.setenv_violations(
        text, managed_envs=_MANAGED, provider_keys=frozenset(), live_reads=frozenset()
    )


def test_a_managed_variable_set_in_a_test_is_one_finding() -> None:
    text = f'\ndef test_x(monkeypatch):\n    monkeypatch.setenv("{_NAME}", "x")\n'
    assert _violations(text) == [
        (3, f'monkeypatch.setenv("{_NAME}", "x")', f"setenv-managed:{_NAME}")
    ]


def test_delenv_is_judged_like_setenv() -> None:
    text = f'\nmonkeypatch.delenv("{_NAME}", raising=False)\n'
    assert [kind for _, _, kind in _violations(text)] == [f"setenv-managed:{_NAME}"]


def test_an_unmanaged_provider_or_live_read_variable_is_not_a_finding() -> None:
    text = (
        '\nmonkeypatch.setenv("HOME", "x")\n'
        'monkeypatch.setenv("ANTHROPIC_API_KEY", "x")\n'
        'monkeypatch.setenv("MEIVA_TOKEN", "x")\n'
    )
    assert (
        plan.setenv_violations(
            text,
            managed_envs=_MANAGED,
            provider_keys=frozenset({"ANTHROPIC_API_KEY", "HOME", "MEIVA_TOKEN"}),
            live_reads=frozenset(),
        )
        == []
    )


def test_a_live_read_variable_is_exempt_even_when_managed() -> None:
    text = '\nmonkeypatch.setenv("MEIVA_TOKEN", "x")\n'
    assert (
        plan.setenv_violations(
            text,
            managed_envs=_MANAGED,
            provider_keys=frozenset(),
            live_reads=frozenset({"MEIVA_TOKEN"}),
        )
        == []
    )


def test_a_comment_is_not_code() -> None:
    assert _violations(f'# monkeypatch.setenv("{_NAME}", "x")\n') == []


def test_a_line_inside_a_docstring_is_not_code() -> None:
    body = (
        f'\ndef test_x():\n    {_QUOTES}\n    monkeypatch.setenv("{_NAME}", "x")\n    {_QUOTES}\n'
    )
    assert _violations(body) == []


def test_a_direct_environment_assignment_is_not_judged() -> None:
    assert _violations(f'os.environ["{_NAME}"] = "x"\n') == []


def test_a_finding_names_the_variable_and_what_to_do_instead() -> None:
    finding = plan.finding(_ROOT / "base/tests/test_x.py", _ROOT, 12, f"setenv-managed:{_NAME}")
    assert finding.startswith(f"base/tests/test_x.py:12: monkeypatch.setenv/delenv on `{_NAME}`")
    assert "Settings is a singleton" in finding
    assert 'monkeypatch.setattr(settings, "<field_name>", value)' in finding


def test_the_summary_names_the_count_and_where_the_exemptions_live() -> None:
    assert plan.summary(3) == (
        "\n3 violations total. See the note at the top of "
        "meizel/state/no_os_environ.py for the rationale and meizel.policy.toml's "
        "[no-os-environ] table for the exemptions."
    )
