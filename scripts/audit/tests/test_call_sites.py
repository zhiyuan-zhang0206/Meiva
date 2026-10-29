# meiva:note
# Locks `scripts/audit/call_sites.py` and the judge behind it: a call is judged by the argument it
# carries, and an argument list the judge cannot read is reported as unproven rather than counted
# either way.
# meiva:link scripts/audit/docs/call-sites.meiva.okf.md

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.audit import call_sites as _shell
from scripts.audit.analysis.calls import (
    EMBEDDED,
    MISSING,
    NAMED_BY_STRING,
    UNPROVEN,
    judge_calls,
    parse_call_spec,
)


def _judge(
    *files: tuple[str, str], specs: str = "build:home"
) -> tuple[list[tuple[str, int, str]], int]:
    findings, judged = judge_calls(files, [parse_call_spec(specs)])
    return [(finding.path, finding.line, finding.verdict) for finding in findings], judged


# meiva:note
# The parameter arrives by name: nothing to report, and the call is still counted. A zero count
# under a spec that names a real function is the reading that says the search did not happen.
def test_a_keyword_argument_satisfies_the_parameter() -> None:
    files = [("caller/go.py", "from base.mod import build\n\n\ndef go():\n    build(home=1)\n")]
    assert _judge(*files) == ([], 1)


def test_a_call_without_the_parameter_is_reported_as_missing() -> None:
    assert _judge(("caller/go.py", "def go(build):\n    build()\n")) == (
        [("caller/go.py", 2, MISSING)],
        1,
    )


# meiva:note
# The same call, read twice: a positional argument is the parameter only when the spec says which
# position it occupies. Without that the judge must not guess that position 0 means `home`.
def test_a_positional_argument_counts_only_when_the_spec_names_the_position() -> None:
    files = [("caller/go.py", "def go(build):\n    build(1)\n")]
    assert _judge(*files, specs="build:home:0") == ([], 1)
    assert _judge(*files) == ([("caller/go.py", 2, MISSING)], 1)


# meiva:note
# An expanded argument list proves nothing in either direction, so it is never reported as missing:
# an `--enforce` run stays red only for a call that demonstrably cannot carry the parameter.
@pytest.mark.parametrize("call", ["build(*args)", "build(**kwargs)"])
def test_an_expanded_argument_list_is_unproven_not_missing(call: str) -> None:
    assert _judge(("caller/go.py", f"def go(build, args, kwargs):\n    {call}\n")) == (
        [("caller/go.py", 2, UNPROVEN)],
        1,
    )


# meiva:note
# `module` narrows to files that can reach it, and the count is what shows the narrowing did not
# silently take the whole tree with it: an unrelated same-named call is still counted out of it.
def test_a_module_spec_skips_files_that_cannot_reach_it() -> None:
    files = (
        ("caller/reaches.py", "from base.mod import build\n\n\ndef go():\n    build()\n"),
        ("caller/unrelated.py", "def go(build):\n    build()\n"),
    )
    assert _judge(*files, specs="base.mod:build:home") == ([("caller/reaches.py", 5, MISSING)], 1)
    assert _judge(*files)[1] == 2


# meiva:note
# A spec without a module matches the bare name everywhere, which is the over-report to expect from
# a common name: the test names it so the choice between the two forms stays visible.
def test_a_bare_name_spec_matches_the_function_name_in_every_file() -> None:
    findings, judged = _judge(
        ("caller/a.py", "def a(build):\n    build(home=1)\n"),
        ("caller/b.py", "def b(build):\n    build()\n"),
    )
    assert judged == 2 and [path for path, _, _ in findings] == ["caller/b.py"]


# meiva:note
# A dotted callee is the same function (`emitter.cluster_label(home=...)`); a spec that could not
# see it would pass a real call site, so the name comes from the attribute when there is one.
def test_a_dotted_callee_is_judged_by_its_attribute_name() -> None:
    files = [("caller/go.py", "from base.mod import emitter\n\n\ndef go():\n    emitter.build()\n")]
    assert _judge(*files) == ([("caller/go.py", 5, MISSING)], 1)


# meiva:note
# A file that is not Python is skipped by the caller, and one that is broken Python does not stop
# the run: a generated source with a syntax error has no call site to judge.
def test_a_source_that_does_not_parse_is_skipped() -> None:
    assert _judge(
        ("caller/broken.py", "def go(\n"), ("caller/go.py", "def go(build):\n    build()\n")
    ) == (
        [("caller/go.py", 2, MISSING)],
        1,
    )


def _judge_full(
    *files: tuple[str, str], specs: str = "build:home"
) -> list[tuple[str, int, str, str]]:
    findings, _ = judge_calls(files, [parse_call_spec(specs)])
    return [(finding.path, finding.line, finding.verdict, finding.where) for finding in findings]


# meiva:note
# The regression for the shape that reached the merge queue: a `pytester` fixture writes a test
# module into a triple-quoted string and runs it as a subprocess, so the call inside is a string
# constant and no AST walk of the fixture file ever sees a call node. The finding is reported at the
# line of the literal, which is the line a reader can open.
def test_a_call_written_inside_a_pytester_string_is_judged() -> None:
    source = (
        "def test_x(pytester):\n"
        "    pytester.makepyfile(\n"
        '        """\n'
        "import telemetry\n"
        "\n"
        "def test_inner():\n"
        "    telemetry.init_telemetry(pipeline=None)\n"
        '"""\n'
        "    )\n"
    )
    assert _judge_full(
        ("tests/fixtures/ops/pytester_fixture.py", source), specs="init_telemetry:home"
    ) == [("tests/fixtures/ops/pytester_fixture.py", 7, MISSING, EMBEDDED)]


# meiva:note
# A string that looks like a module but does not parse still names the call it was meant to make, so
# it is unproven rather than silent: silence is what let the queue's fixture pass as "no call sites".
def test_an_embedded_source_that_does_not_parse_is_unproven() -> None:
    source = 'def test_x(pytester):\n    pytester.makepyfile(\n        """\ndef test_inner(:\n    build(\n"""\n    )\n'
    assert _judge_full(("caller/broken_fixture.py", source)) == [
        ("caller/broken_fixture.py", 3, UNPROVEN, EMBEDDED)
    ]


# meiva:note
# The other side of reading strings: prose that names a call is not a call site. Only a literal that
# starts a definition or an import at the beginning of a line is read as a module.
def test_a_prose_string_that_mentions_a_call_is_not_a_finding() -> None:
    source = (
        "def go():\n"
        '    """Call build() once the module is ready.\n'
        "\n"
        "    The pipeline is built by hand here.\n"
        '    """\n'
    )
    assert _judge_full(("caller/prose.py", source)) == []


# meiva:note
# `getattr(module, "build")(...)` names the function in a string, so the argument list at that line
# says nothing about the parameter. It is recorded as unproven rather than passed over.
def test_a_call_reached_through_getattr_is_unproven() -> None:
    source = 'def go(module):\n    getattr(module, "build")(pipeline=None)\n'
    assert _judge_full(("caller/dynamic.py", source)) == [
        ("caller/dynamic.py", 2, UNPROVEN, NAMED_BY_STRING)
    ]


@pytest.mark.parametrize(
    ("text", "module", "function", "parameter", "position"),
    [
        ("build:home", None, "build", "home", None),
        ("build:home:0", None, "build", "home", 0),
        ("base.mod:build:home", "base.mod", "build", "home", None),
        ("base.mod:build:home:2", "base.mod", "build", "home", 2),
    ],
)
def test_a_spec_parses_into_the_module_function_parameter_and_position(
    text: str, module: str | None, function: str, parameter: str, position: int | None
) -> None:
    spec = parse_call_spec(text)
    assert (spec.module, spec.function, spec.parameter, spec.position) == (
        module,
        function,
        parameter,
        position,
    )


@pytest.mark.parametrize("text", ["build", "a:b:c:d", "a:b:c:d:e", "build:"])
def test_a_spec_that_does_not_parse_is_an_error_not_an_empty_search(text: str) -> None:
    with pytest.raises(ValueError):
        parse_call_spec(text)


def _tree(tmp_path: Path, text: str = "def go(build):\n    build()\n") -> Path:
    (tmp_path / "caller").mkdir()
    (tmp_path / "caller" / "go.py").write_text(text, encoding="utf-8")
    return tmp_path


# meiva:note
# The default reports and exits 0: a caller may be right to leave the parameter to a wrapper, and
# that judgment belongs to the author. `--enforce` is the opt-in that makes the same finding fail.
def test_the_default_run_reports_without_failing(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _shell.main(["--root", str(_tree(tmp_path)), "--spec", "build:home"]) == 0
    printed = capsys.readouterr().out
    assert "caller/go.py:2:" in printed
    assert "call sites checked: 1; missing: 1; unproven: 0" in printed


def test_enforce_exits_non_zero_for_a_missing_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _shell.main(["--root", str(_tree(tmp_path)), "--spec", "build:home", "--enforce"]) == 1


# meiva:note
# Unproven is not a failure even under `--enforce`; otherwise one `*args` wrapper would decide the
# verdict for every caller behind it.
def test_enforce_does_not_fail_on_an_unproven_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _tree(tmp_path, "def go(build, args):\n    build(*args)\n")
    assert _shell.main(["--root", str(root), "--spec", "build:home", "--enforce"]) == 0
    assert "unproven: 1" in capsys.readouterr().out


def test_json_output_carries_the_counts_and_the_verdicts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert _shell.main(["--root", str(_tree(tmp_path)), "--spec", "build:home", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checked"] == 1
    assert payload["findings"] == [
        {
            "path": "caller/go.py",
            "line": 2,
            "function": "build",
            "parameter": "home",
            "verdict": MISSING,
            "where": "",
        }
    ]


def test_a_run_without_a_spec_is_a_usage_error() -> None:
    with pytest.raises(SystemExit) as exit_info:
        _shell.main([])
    assert exit_info.value.code == 2
