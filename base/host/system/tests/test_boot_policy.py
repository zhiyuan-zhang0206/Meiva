from __future__ import annotations

import json
from pathlib import Path

import pytest

from base.host.system import boot_policy


# meiva:note
# The host's boot timestamp as the caller reads it, so a test can move it.
class _Boot:
    def __init__(self) -> None:
        self.time = 1_000_000.0

    def begin(self, home: Path) -> int:
        return boot_policy.begin_attempt(home, boot_time=self.time)


@pytest.fixture()
def boot_time() -> _Boot:
    return _Boot()


def test_attempts_count_up_within_one_boot(tmp_path: Path, boot_time: _Boot) -> None:
    assert [boot_time.begin(tmp_path) for _ in range(3)] == [1, 2, 3]


def test_the_count_survives_clock_jitter_of_the_boot_time(tmp_path: Path, boot_time: _Boot) -> None:
    boot_time.begin(tmp_path)
    boot_time.time += 1.5
    assert boot_time.begin(tmp_path) == 2


def test_a_new_boot_starts_the_count_again(tmp_path: Path, boot_time: _Boot) -> None:
    for _ in range(boot_policy.BOOT_MAX_ATTEMPTS):
        boot_time.begin(tmp_path)
    boot_time.time += 3600
    assert boot_time.begin(tmp_path) == 1


def test_success_clears_the_count(tmp_path: Path, boot_time: _Boot) -> None:
    boot_time.begin(tmp_path)
    boot_policy.clear_attempts(tmp_path)
    boot_policy.clear_attempts(tmp_path)
    assert boot_time.begin(tmp_path) == 1


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"attempts": 4}', '{"boot_time": "x"}'])
def test_an_unreadable_record_counts_again_instead_of_failing_every_boot(
    tmp_path: Path, boot_time: _Boot, content: str
) -> None:
    path = tmp_path / "run" / "boot-attempts.json"
    path.parent.mkdir()
    path.write_text(content)
    assert boot_time.begin(tmp_path) == 1
    assert json.loads(path.read_text())["attempts"] == 1


def test_the_budget_is_spent_by_the_last_allowed_attempt() -> None:
    assert not boot_policy.budget_spent(boot_policy.BOOT_MAX_ATTEMPTS - 1)
    assert boot_policy.budget_spent(boot_policy.BOOT_MAX_ATTEMPTS)
    assert boot_policy.budget_spent(boot_policy.BOOT_MAX_ATTEMPTS + 1)


@pytest.mark.parametrize(("macos", "expected"), [(True, 0), (False, 75)])
def test_only_launchd_needs_a_zero_exit_to_stop_retrying(macos: bool, expected: int) -> None:
    assert boot_policy.exit_code_after_budget(75, macos=macos) == expected
