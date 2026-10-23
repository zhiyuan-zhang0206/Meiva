from __future__ import annotations

from typing import Any

from scripts.ci.pull_requests import status
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    TEST_CLOCK,
    CIStatus,
    status_check_rollup_entry,
)
from scripts.ci.pull_requests.tests.test_ci_monitor import (
    gh as gh,
)


def test_pre_expansion_cancelled_run_does_not_poison_the_head(gh: Any) -> None:
    cancelled_at = "2026-10-12T15:45:03Z"
    succeeded_at = "2026-10-12T15:58:00Z"
    host = gh(
        [
            status_check_rollup_entry(
                "backend shard (${{ matrix.group }}/16)", "CANCELLED", completed_at=cancelled_at
            ),
            status_check_rollup_entry(
                "e2e shard (${{ matrix.group }}/4)", "CANCELLED", completed_at=cancelled_at
            ),
            status_check_rollup_entry(
                "backend (pytest + pyright)", "CANCELLED", completed_at=cancelled_at
            ),
            status_check_rollup_entry(
                "backend (pytest + pyright)", "SUCCESS", completed_at=succeeded_at
            ),
            status_check_rollup_entry("backend shard (3/16)", "SUCCESS", completed_at=succeeded_at),
            status_check_rollup_entry("e2e shard (1/4)", "SUCCESS", completed_at=succeeded_at),
        ]
    )
    r = status.check_ci("5262", host=host, clock=TEST_CLOCK, workflows=True)
    assert r.verdict is CIStatus.ALL_PASSED
    assert r.failed == []
    assert r.completed == [
        "backend (pytest + pyright)",
        "backend shard (3/16)",
        "e2e shard (1/4)",
    ]


def test_named_cancellation_without_a_successor_stays_red(gh: Any) -> None:
    host = gh(
        [
            status_check_rollup_entry("backend shard (3/16)", "CANCELLED"),
            status_check_rollup_entry("backend shard (1/16)", "SUCCESS"),
        ]
    )
    r = status.check_ci("1", host=host, clock=TEST_CLOCK, workflows=True)
    assert r.verdict is CIStatus.FAILED
    assert r.failed == [{"name": "backend shard (3/16)", "conclusion": "CANCELLED"}]
