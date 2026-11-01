from __future__ import annotations

from pathlib import Path

import pytest

from meizel.tests.synthetic import RepoFactory, run

ROOT = '[roles]\napplication = ["app"]\ntests = ["tests"]\n'


def _repo(make_repo: RepoFactory, files: dict[str, str]) -> Path:
    return make_repo({"meizel.toml": ROOT, "app/__init__.py": "", "tests/__init__.py": "", **files})


# meiva:note
# The `debt propagation` measurement as the command printed it, without the `meizel: ` prefix of
# every line. It reads the governed production code and writes nothing.
def measured(root: Path, capsys: pytest.CaptureFixture[str], *arguments: str) -> list[str]:
    assert run(root, "debt", "propagation", *arguments) == 0
    return [line.removeprefix("meizel: ") for line in capsys.readouterr().out.splitlines()]


def test_a_call_of_the_function_that_acquires_the_world_is_a_site(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(
        make_repo,
        {
            "app/mod.py": (
                "import os\n\n"
                "def read_pid() -> int:\n    return os.getpid()\n\n"
                "def caller() -> int:\n    return read_pid()\n"
            )
        },
    )
    lines = measured(root, capsys)
    assert lines[0].startswith("world reads that travel through a function: 1 sites")
    assert "app/mod.py:7  -> app.mod.read_pid" in lines


def test_a_call_through_an_imported_name_is_a_site(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(
        make_repo,
        {
            "app/one.py": "import os\n\ndef acquire() -> str:\n    return os.environ['A']\n",
            "app/two.py": "from app.one import acquire\n\ndef use() -> str:\n    return acquire()\n",
        },
    )
    assert "app/two.py:4  -> app.one.acquire" in measured(root, capsys)


def test_a_call_through_an_imported_module_is_a_site(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(
        make_repo,
        {
            "app/one.py": "import os\n\ndef acquire() -> str:\n    return os.environ['A']\n",
            "app/three.py": "from app import one\n\ndef use() -> str:\n    return one.acquire()\n",
        },
    )
    assert "app/three.py:4  -> app.one.acquire" in measured(root, capsys)


def test_a_read_that_travels_two_calls_is_measured_at_both(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(
        make_repo,
        {
            "app/mod.py": (
                "import os\n\n"
                "def read_pid() -> int:\n    return os.getpid()\n\n"
                "def wrapped() -> int:\n    return read_pid()\n\n"
                "def caller() -> int:\n    return wrapped()\n"
            )
        },
    )
    lines = measured(root, capsys)
    assert "app/mod.py:7  -> app.mod.read_pid" in lines
    assert "app/mod.py:10  -> app.mod.wrapped" in lines
    assert any("app.mod.wrapped" in line and "through a call" in line for line in lines)


def test_a_call_of_a_function_that_reads_nothing_is_not_a_site(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(
        make_repo,
        {
            "app/plain.py": (
                "def value() -> int:\n    return 1\n\ndef use() -> int:\n    return value()\n"
            )
        },
    )
    lines = measured(root, capsys)
    assert lines[0].startswith("world reads that travel through a function: 0 sites")
    assert not any("->" in line for line in lines)


def test_a_test_file_call_is_not_a_site(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(
        make_repo,
        {
            "app/four.py": "import os\n\ndef acquire() -> str:\n    return os.environ['A']\n",
            "tests/test_four.py": (
                "from app.four import acquire\n\n\ndef test_it() -> None:\n    assert acquire()\n"
            ),
        },
    )
    lines = measured(root, capsys)
    assert lines[0].startswith("world reads that travel through a function: 0 sites")
    assert not any("tests/test_four.py" in line for line in lines)


def test_the_measurement_writes_no_baseline(
    make_repo: RepoFactory, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(
        make_repo,
        {
            "app/mod.py": (
                "import os\n\ndef read_pid() -> int:\n    return os.getpid()\n\n"
                "def caller() -> int:\n    return read_pid()\n"
            )
        },
    )
    measured(root, capsys)
    manifest = root / "app/meizel.toml"
    assert "[debt" not in (manifest.read_text() if manifest.exists() else "")
