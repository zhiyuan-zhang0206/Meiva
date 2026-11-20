from __future__ import annotations

import ast
import subprocess
import textwrap
from pathlib import Path

import pytest

from scripts.rules.state import env_presence, env_presence_cli


def _python(source: str) -> list[int]:
    return [line for line, _ in env_presence.python_findings(ast.parse(textwrap.dedent(source)))]


# meiva:note
# The five samples the rule was asked for: three presence tests it reports and two value tests it
# leaves alone.
@pytest.mark.parametrize(
    ("source", "line"),
    [
        ('import os\nif "OPENAI_API_KEY" in os.environ:\n    pass\n', 2),
        (
            'import os\ntry:\n    key = os.environ["OPENAI_API_KEY"]\nexcept KeyError:\n    key = None\n',
            3,
        ),
    ],
)
def test_a_python_presence_test_is_reported(source: str, line: int) -> None:
    assert _python(source) == [line]


def test_a_shell_presence_test_is_reported() -> None:
    assert [
        line for line, _ in env_presence.shell_findings("if [ -v OPENAI_API_KEY ]; then\n")
    ] == [1]


def test_a_python_value_test_is_silent() -> None:
    assert _python('import os\nif os.environ.get("OPENAI_API_KEY"):\n    pass\n') == []


def test_a_shell_value_test_is_silent() -> None:
    assert env_presence.shell_findings('if [ -n "${OPENAI_API_KEY:-}" ]; then\n') == []


@pytest.mark.parametrize(
    "source",
    [
        'import os\nif "X" not in os.environ:\n    pass\n',
        'from os import environ\nif "X" in environ:\n    pass\n',
        'import os\nif os.environ.get("X") is not None:\n    pass\n',
        'import os\nif os.getenv("X") is None:\n    pass\n',
        'import os\nname = "X"\nif name in os.environ:\n    pass\n',
        "if [[ -v X ]]; then :; fi\n",
    ],
)
def test_every_presence_shape_is_reported(source: str) -> None:
    found = (
        env_presence.shell_findings(source)
        if source.startswith("if [[")
        else env_presence.python_findings(ast.parse(source))
    )
    assert len(found) == 1


@pytest.mark.parametrize(
    "source",
    [
        'import os\nvalue = os.environ.get("X") or "default"\n',
        'import os\nvalue = os.environ["X"]\n',
        'import os\ntry:\n    value = int(os.environ.get("X", "1"))\nexcept ValueError:\n    value = 1\n',
        'environ = {}\nif "X" in environ:\n    pass\n',
        'import os\nnames = [key for key in os.environ if key.startswith("A_")]\n',
    ],
)
def test_reads_that_are_not_presence_tests_are_silent(source: str) -> None:
    assert env_presence.python_findings(ast.parse(source)) == []


# meiva:note
# A git checkout of `files`, built with fixed git argv in the test's own directory.
def _repo(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    for args in (["init", "-q"], ["add", "-A"]):
        subprocess.run(["git", "-C", str(tmp_path), *args], check=True)  # noqa: S603
    return tmp_path


# meiva:note
# The CLI judges production Python, shell scripts and workflow files, skips the tests, and honours the
# policy's `[env-presence] files` exemptions while failing a stale one.
def test_the_cli_judges_the_tracked_files_and_honours_the_policy(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    policy = (
        '[sections.env-presence]\nfiles = "reasons"\n\n'
        '[env-presence.files]\n"lib/exempt.py" = "probes a variable an operator sets empty on purpose"\n'
    )
    presence = 'import os\nif "X" in os.environ:\n    pass\n'
    root = _repo(
        tmp_path,
        {
            "meizel.policy.toml": policy,
            "lib/a.py": presence,
            "lib/exempt.py": presence,
            "lib/tests/test_a.py": presence,
            "ops/run.sh": "[ -v X ] && echo set\n",
            ".github/workflows/ci.yml": "jobs:\n  a:\n    steps:\n      - run: '[[ -v X ]]'\n",
        },
    )
    assert env_presence_cli.main([], repo_root=root) == 1
    err = capsys.readouterr().err
    assert "lib/a.py:2:" in err
    assert "ops/run.sh:1:" in err
    assert ".github/workflows/ci.yml:4:" in err
    assert "lib/exempt.py" not in err
    assert "lib/tests/test_a.py" not in err
    (root / "lib/exempt.py").write_text('import os\nif os.environ.get("X"):\n    pass\n')
    assert env_presence_cli.main([], repo_root=root) == 1
    assert "stale [env-presence] files entry lib/exempt.py" in capsys.readouterr().err
