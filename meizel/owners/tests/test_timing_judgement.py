from __future__ import annotations

from pathlib import Path

import pytest

from meizel.owners import timing_judgement as _rule
from meizel.owners import timing_judgement_cli as _lint

_NEW_SITE = (
    "import time\n"
    "\n"
    "def test_reports_progress() -> None:\n"
    "    started = time.monotonic()\n"
    "    work()\n"
    "    elapsed = time.monotonic() - started\n"
    "    assert elapsed < 0.5\n"
)


def _findings(src: str, rel: str = "tests/test_probe.py") -> list[str]:
    return [message for _, message in _rule.findings_in_source(src, rel)]


# meiva:note
# The negative control the rule is judged by: the exact assertion #7725 kept green under the
# defect it was written to catch.
def test_an_upper_bound_on_a_measured_duration_is_a_finding() -> None:
    found = _findings(_NEW_SITE)
    assert len(found) == 1
    assert "test_reports_progress" in found[0]
    assert "elapsed < 0.5" in found[0]


# meiva:note
# A lower bound is the same mistake facing the other way: it fails a correct, faster implementation.
def test_a_lower_bound_on_a_measured_duration_is_a_finding() -> None:
    src = _NEW_SITE.replace("assert elapsed < 0.5", "assert elapsed > 0.1")
    assert len(_findings(src)) == 1


# meiva:note
# The same read imported by name: `from time import monotonic` is `time.monotonic()` with the owner
# dropped, and it must be judged the same way — 2026-10-30, a 30 s settle deadline spelled this way
# was invisible to the rule.
def test_a_clock_imported_by_name_is_a_wall_clock() -> None:
    src = (
        "from time import monotonic\n"
        "\n"
        "def test_wait() -> None:\n"
        "    started = monotonic()\n"
        "    assert monotonic() - started < 2.0\n"
    )
    assert len(_findings(src)) == 1


def test_a_clock_imported_under_another_name_is_a_wall_clock() -> None:
    src = (
        "from time import monotonic as now\n"
        "\n"
        "def test_wait() -> None:\n"
        "    started = now()\n"
        "    assert now() - started < 2.0\n"
    )
    assert len(_findings(src)) == 1


# meiva:note
# Negative controls for the import read: a name from `time` that is not a clock, and a local function
# that merely shares a clock's name, are not judged.
def test_a_name_imported_from_time_that_is_not_a_clock_is_ignored() -> None:
    src = "from time import sleep\n\ndef test_wait() -> None:\n    sleep(0.1)\n    assert 1 == 1\n"
    assert _findings(src) == []


def test_a_local_function_named_like_a_clock_is_ignored() -> None:
    src = (
        "def monotonic() -> float:\n"
        "    return 0.0\n"
        "\n"
        "def test_wait() -> None:\n"
        "    assert monotonic() < 2.0\n"
    )
    assert _findings(src) == []


def test_a_clock_read_inside_the_assert_is_a_finding() -> None:
    src = (
        "import time\n"
        "\n"
        "def test_wait() -> None:\n"
        "    started = time.monotonic()\n"
        "    assert time.monotonic() - started < 2.0\n"
    )
    assert len(_findings(src)) == 1


def test_a_loop_clock_is_a_wall_clock() -> None:
    src = (
        "def test_wait(loop) -> None:\n"
        "    started = loop.time()\n"
        "    assert loop.time() - started < 2.0\n"
    )
    assert len(_findings(src)) == 1


# meiva:note
# The positive control: the marker buys silence on the line it sits on and on the line below it, and
# only there -- a marker two lines up is about something else. `_ASSERT_LINE` is the index of
# `    assert elapsed < 0.5` inside `_NEW_SITE`.
_ASSERT_LINE = 6


def test_the_marker_on_the_asserts_own_line_silences_it() -> None:
    lines = _NEW_SITE.splitlines()
    lines[_ASSERT_LINE] += "  # timing-guard: only catches a hang"
    assert _findings("\n".join(lines) + "\n") == []


def test_the_marker_on_the_line_above_silences_it() -> None:
    lines = _NEW_SITE.splitlines()
    lines.insert(_ASSERT_LINE, "    # timing-guard: only catches a hang")
    assert _findings("\n".join(lines) + "\n") == []


def test_a_marker_two_lines_above_does_not_count() -> None:
    lines = _NEW_SITE.splitlines()
    lines.insert(_ASSERT_LINE - 1, "    # timing-guard: only catches a hang")
    assert len(_findings("\n".join(lines) + "\n")) == 1


def test_a_clock_handed_to_a_timeout_is_not_a_judgement() -> None:
    src = (
        "import asyncio\n"
        "import time\n"
        "\n"
        "async def test_wait() -> None:\n"
        "    assert await asyncio.wait_for(work(), timeout=time.monotonic() + 1) is None\n"
    )
    assert _findings(src) == []


def test_a_counted_fact_is_not_a_judgement() -> None:
    src = "def test_envelopes() -> None:\n    assert len(sent) == 3\n    assert rounds == 2\n"
    assert _findings(src) == []


def test_a_carried_clock_is_not_a_duration() -> None:
    src = (
        "import time\n"
        "\n"
        "async def test_wait() -> None:\n"
        "    expected = await work(timeout=time.monotonic() + 1)\n"
        "    assert expected == 3\n"
    )
    assert _findings(src) == []


# meiva:note
# The baseline is what keeps the gate off every pre-existing site: the same comparison keyed
# under its file and function stops being reported, while a second one in that function does not.
def test_a_baseline_entry_silences_its_own_site_only() -> None:
    key = ("tests/test_probe.py", "test_reports_progress", "elapsed < N")
    assert _rule.findings_in_source(_NEW_SITE, key[0], baseline=frozenset({key})) == []
    added = _NEW_SITE.replace(
        "    assert elapsed < 0.5\n", "    assert elapsed < 0.5\n    assert elapsed > 0.25\n"
    )
    assert len(_rule.findings_in_source(added, key[0], baseline=frozenset({key}))) == 1


def test_a_retuned_constant_keeps_its_baseline_entry() -> None:
    key = ("tests/test_probe.py", "test_reports_progress", "elapsed < N")
    retuned = _NEW_SITE.replace("elapsed < 0.5", "elapsed < 0.05")
    assert _rule.findings_in_source(retuned, key[0], baseline=frozenset({key})) == []


# meiva:note
# The gate, end to end on a throwaway repo: a test file with the new assertion reds the run, the
# same file with the marker passes it, and the baseline is what a `--write-baseline` run records.
def _repo(tmp_path: Path, source: str) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n', encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_probe.py").write_text(source, encoding="utf-8")
    return tmp_path


def test_the_gate_turns_red_on_a_new_site_and_green_with_the_marker(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, _NEW_SITE)
    assert _lint.main([], repo_root=root) == 1
    assert "tests/test_probe.py:7: error:" in capsys.readouterr().out
    (root / "tests" / "test_probe.py").write_text(
        _NEW_SITE.replace(
            "assert elapsed < 0.5", "assert elapsed < 0.5  # timing-guard: only catches a hang"
        ),
        encoding="utf-8",
    )
    assert _lint.main([], repo_root=root) == 0


def test_writing_the_baseline_silences_the_existing_site(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    root = _repo(tmp_path, _NEW_SITE)
    assert _lint.main(["--write-baseline"], repo_root=root) == 0
    assert _lint.main([], repo_root=root) == 0
    assert "1 baseline site(s)" in capsys.readouterr().out


def test_a_missing_target_path_is_an_error(tmp_path: Path) -> None:
    root = _repo(tmp_path, _NEW_SITE)
    assert _lint.main(["tests/absent.py"], repo_root=root) == 1
