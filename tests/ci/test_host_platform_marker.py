# meiva:note
# Locks `tests/fixtures/host_platform.py`: `requires_host` skips the other host and says why.
#
# The plugin is the only place a platform-specific test may be skipped: the test names the
# host it needs and this module turns that into the skip, so a reader sees the declaration
# rather than a `skipif(sys.platform ...)` nobody rereads.
# meiva:link scripts/lint/docs/host-platform-marks.meiva.okf.md

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from typing import cast

import pytest

from tests.fixtures.host_platform import required_host

_REPO_ROOT = Path(__file__).resolve().parents[2]


class _Item:
    nodeid = "tests/test_x.py::test_x"

    def __init__(self, marks: list[pytest.Mark]) -> None:
        self._marks = marks
        self.added: list[pytest.Mark] = []

    def iter_markers(self, name: str | None = None) -> list[pytest.Mark]:
        return [mark for mark in self._marks if mark.name == name]

    def add_marker(self, mark: pytest.Mark) -> None:
        self.added.append(mark)


def _marker(*args: object) -> pytest.Mark:
    return pytest.mark.requires_host(*args).mark


def _item(*marks: pytest.Mark) -> pytest.Item:
    return cast(pytest.Item, _Item(list(marks)))


def test_an_item_without_the_marker_needs_no_host() -> None:
    assert required_host(_item()) is None


def test_the_marker_names_its_host() -> None:
    assert required_host(_item(_marker("darwin"))) == "darwin"


@pytest.mark.parametrize(
    "args",
    [(), ("windows",), ("darwin", "linux"), (1,)],
    ids=["no-argument", "unknown-system", "two-systems", "not-a-string"],
)
def test_a_marker_this_module_cannot_honour_fails_collection(args: tuple[object, ...]) -> None:
    with pytest.raises(pytest.UsageError):
        required_host(_item(_marker(*args)))


def test_two_markers_on_one_item_fail_collection() -> None:
    with pytest.raises(pytest.UsageError):
        required_host(_item(_marker("darwin"), _marker("linux")))


# meiva:note
# A real pytest run of a synthetic suite: the plugin is loaded by `-p`, so the inner run
# exercises collection the way the repository's own conftest registers it.
def _run(tmp_path: Path, body: str) -> subprocess.CompletedProcess[str]:
    (tmp_path / "test_inner.py").write_text(body, encoding="utf-8")
    env = dict(os.environ)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    env.pop("PYTEST_ADDOPTS", None)
    return subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-rs",
            "-p",
            "no:cacheprovider",
            "-p",
            "tests.fixtures.host_platform",
            "-o",
            "markers=requires_host",
            str(tmp_path / "test_inner.py"),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_the_named_host_runs_and_every_other_host_skips_with_the_reason(tmp_path: Path) -> None:
    from base.native_process.os_platform import Platform

    # meiva:note
    # host-ok: the assertion is about the skip this host produces, so the host is an input.
    current = Platform.current().system
    other = "linux" if current == "darwin" else "darwin"
    result = _run(
        tmp_path,
        "import pytest\n\n\n"
        '@pytest.mark.requires_host("' + current + '")\n'
        "def test_here():\n    pass\n\n\n"
        '@pytest.mark.requires_host("' + other + '")\n'
        "def test_there():\n    pass\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "1 passed, 1 skipped" in result.stdout
    assert f"requires a {other} host: this one is {current}" in result.stdout
