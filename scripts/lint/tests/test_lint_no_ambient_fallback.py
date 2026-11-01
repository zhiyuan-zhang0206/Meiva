# meiva:note
# Unit tests for scripts/lint/diagnostics/no_ambient_fallback.py: callee ambient fallback detection,
# Diagnostic-as-Prompt formatting, and CLI handling.

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from scripts.lint import code_structure as lcs
from scripts.lint.diagnostics import no_ambient_fallback as naf


def test_or_fallback_to_datetime_now_is_flagged() -> None:
    code = """
from datetime import datetime

def send_event(now: datetime | None = None):
    now = now or datetime.now()
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/events.py")
    assert len(violations) == 1
    line, message = violations[0]
    assert line == 5
    assert "ambient fallback for parameter 'now' to datetime.now() is forbidden" in message
    assert "code-placement-and-purity.md#L55" in message
    assert "Defaults on parameters are reserved for pure constants" in message
    assert "Pass the value explicitly from the composition root instead" in message


def test_if_none_assign_datetime_now_is_flagged() -> None:
    code = """
from datetime import datetime

def schedule_task(*, now: datetime | None = None):
    if now is None:
        now = datetime.now()
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/tasks.py")
    assert len(violations) == 1
    line, message = violations[0]
    assert line == 6
    assert "ambient fallback for parameter 'now' to datetime.now() is forbidden" in message


def test_if_not_param_assign_os_environ_is_flagged() -> None:
    code = """
import os
from collections.abc import Mapping

def configure(env: Mapping[str, str] | None = None):
    if not env:
        env = os.environ
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/config.py")
    assert len(violations) == 1
    line, message = violations[0]
    assert line == 7
    assert "ambient fallback for parameter 'env' to os.environ is forbidden" in message


def test_or_fallback_to_os_environ_is_flagged() -> None:
    code = """
import os
from collections.abc import Mapping

def configure(env: Mapping[str, str] | None = None):
    env = env or os.environ
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/config.py")
    assert len(violations) == 1
    assert violations[0][0] == 6
    assert "ambient fallback for parameter 'env' to os.environ is forbidden" in violations[0][1]


def test_ternary_if_none_is_flagged() -> None:
    code = """
from datetime import datetime

def run(now: datetime | None = None):
    now = datetime.now() if now is None else now
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/runner.py")
    assert len(violations) == 1
    assert violations[0][0] == 5
    assert "ambient fallback for parameter 'now' to datetime.now() is forbidden" in violations[0][1]


def test_ternary_if_not_none_is_flagged() -> None:
    code = """
from datetime import datetime

def run(now: datetime | None = None):
    now = now if now is not None else datetime.now()
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/runner.py")
    assert len(violations) == 1
    assert violations[0][0] == 5
    assert "ambient fallback for parameter 'now' to datetime.now() is forbidden" in violations[0][1]


def test_time_time_fallback_is_flagged() -> None:
    code = """
import time

def sleep_until(deadline: float | None = None):
    if deadline is None:
        deadline = time.time() + 10.0
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/timer.py")
    assert len(violations) == 1
    assert violations[0][0] == 6
    assert (
        "ambient fallback for parameter 'deadline' to time.time() is forbidden" in violations[0][1]
    )


def test_datetime_utcnow_fallback_is_flagged() -> None:
    code = """
from datetime import datetime

def log_event(now: datetime | None = None):
    now = now or datetime.utcnow()
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/log.py")
    assert len(violations) == 1
    assert "datetime.utcnow()" in violations[0][1]


def test_self_attribute_assignment_is_flagged() -> None:
    code = """
from datetime import datetime

class Runner:
    def __init__(self, now: datetime | None = None):
        if now is None:
            self.now = datetime.now()
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/runner.py")
    assert len(violations) == 1
    assert violations[0][0] == 7
    assert "ambient fallback for parameter 'now' to datetime.now() is forbidden" in violations[0][1]


def test_return_or_fallback_is_flagged() -> None:
    code = """
from datetime import datetime

def current(now: datetime | None = None) -> datetime:
    return now or datetime.now()
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/clock.py")
    assert len(violations) == 1
    assert violations[0][0] == 5
    assert "ambient fallback for parameter 'now' to datetime.now() is forbidden" in violations[0][1]


def test_return_ternary_fallback_is_flagged() -> None:
    code = """
from datetime import datetime

def current(now: datetime | None = None) -> datetime:
    return datetime.now() if now is None else now
"""
    tree = ast.parse(code)
    violations = naf.violations(tree, "app/clock.py")
    assert len(violations) == 1
    assert violations[0][0] == 5
    assert "ambient fallback for parameter 'now' to datetime.now() is forbidden" in violations[0][1]


def test_non_ambient_fallback_is_clean() -> None:
    code = """
def process(items: list | None = None, limit: int | None = None, label: str | None = None):
    items = items or []
    limit = limit if limit is not None else 100
    if label is None:
        label = "default"
    return items, limit, label
"""
    tree = ast.parse(code)
    assert naf.violations(tree, "app/clean.py") == []


def test_required_parameter_is_clean() -> None:
    code = """
from datetime import datetime

def send(now: datetime):
    if now is None:
        now = datetime.now()
"""
    tree = ast.parse(code)
    assert naf.violations(tree, "app/clean.py") == []


def test_tuning_number_default_is_clean() -> None:
    code = """
def retry(max_attempts: int = 3, timeout_s: float = 30.0):
    pass
"""
    tree = ast.parse(code)
    assert naf.violations(tree, "app/clean.py") == []


def test_loop_time_is_not_time_time() -> None:
    code = """
import asyncio

async def aclose(deadline: float | None = None):
    if deadline is None:
        deadline = asyncio.get_running_loop().time() + 5.0
"""
    tree = ast.parse(code)
    assert naf.violations(tree, "app/clean.py") == []


def test_test_paths_are_exempt() -> None:
    code = """
from datetime import datetime

def helper(now: datetime | None = None):
    now = now or datetime.now()
"""
    tree = ast.parse(code)
    assert naf.violations(tree, "services/tests/test_support.py") == []
    assert naf.violations(tree, "tests/unit/test_clock.py") == []


def test_check_file_and_main_cli(tmp_path: Path) -> None:
    clean_file = tmp_path / "clean.py"
    clean_file.write_text("def f(x: int = 1): return x\n", encoding="utf-8")

    dirty_file = tmp_path / "dirty.py"
    dirty_file.write_text(
        "import os\ndef f(env=None):\n    env = env or os.environ\n", encoding="utf-8"
    )

    assert naf.check_file(clean_file, tmp_path) == []
    violations = naf.check_file(dirty_file, tmp_path)
    assert len(violations) == 1

    assert naf.main(["--only"]) == 0
    assert naf.main([str(clean_file)], repo_root=tmp_path) == 0
    assert naf.main([str(dirty_file)], repo_root=tmp_path) == 1


@pytest.mark.parametrize(
    ("filename", "is_split"),
    [
        ("foo_part1.py", True),
        ("foo_part2.py", True),
        ("foo_part9.py", True),
        ("bar_part10.py", True),
        ("helpers.py", True),
        ("_helpers.py", True),
        ("foo_helpers.py", False),
        ("blocking_helpers.py", False),
        ("permissions_helper.py", False),
        ("helper.py", False),
        ("contracts.py", False),
        ("pure_core.py", False),
        ("domain.py", False),
    ],
)
def test_is_shallow_split_filename(filename: str, is_split: bool) -> None:
    assert lcs.is_shallow_split_filename(filename) is is_split


def test_check_shallow_split_flags_non_test_file() -> None:
    error = lcs.check_shallow_split("services/agent_runner/engine_part2.py")
    assert error is not None
    assert error.startswith("services/agent_runner/engine_part2.py:1: ")
    assert "mechanical shallow-split filename 'engine_part2.py' is forbidden" in error
    assert "Extract pure data contracts to contracts.py" in error
    assert "Extract strict Pure Core module" in error
    assert "Create a cohesive subpackage with a narrow meizel.toml exports door" in error


def test_check_shallow_split_flags_naked_helpers() -> None:
    error = lcs.check_shallow_split("gateway/routes/helpers.py")
    assert error is not None
    assert "mechanical shallow-split filename 'helpers.py' is forbidden" in error

    error_suffixed = lcs.check_shallow_split("gateway/routes/_helpers.py")
    assert error_suffixed is not None
    assert "mechanical shallow-split filename '_helpers.py' is forbidden" in error_suffixed


def test_check_shallow_split_exempts_test_paths() -> None:
    assert lcs.check_shallow_split("agent/tests/test_compact_part2.py") is None
    assert lcs.check_shallow_split("base/daemon/tests/_health_helpers.py") is None
    assert lcs.check_shallow_split("services/tests/helpers.py") is None


def test_check_shallow_split_allows_clean_names() -> None:
    assert (
        lcs.check_shallow_split("services/supervision/healthchecks/permissions_helper.py") is None
    )
    assert lcs.check_shallow_split("gateway/contracts.py") is None
    assert lcs.check_shallow_split("gateway/core.py") is None
