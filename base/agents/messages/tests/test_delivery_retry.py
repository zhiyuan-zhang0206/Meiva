# meiva:note
# The retry policy answers without the database driver: retry.py decides both
# policies, retry_io.py is the only module that names psycopg, and
# delivery_retry_probe.py is the falsifier for that split.

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from base.agents.messages.delivery.retry import transient_database_failure

_REPO_ROOT = Path(__file__).resolve().parents[4]
_PROBE = Path(__file__).with_name("delivery_retry_probe.py")


def _classify(
    *,
    pool_timeout: bool = False,
    abandoned_restore: bool = False,
    operational_error: bool = False,
    operational_subclass: bool = False,
    sqlstate: str | None = None,
) -> bool:
    return transient_database_failure(
        pool_timeout=pool_timeout,
        abandoned_restore=abandoned_restore,
        operational_error=operational_error,
        operational_subclass=operational_subclass,
        sqlstate=sqlstate,
    )


def test_a_pool_timeout_and_an_abandoned_restore_are_transient() -> None:
    assert _classify(pool_timeout=True)
    assert _classify(abandoned_restore=True)


def test_a_query_error_is_not_transient() -> None:
    assert not _classify(operational_subclass=True, sqlstate="23505")


def test_only_the_exact_driver_error_without_a_sqlstate_is_transient() -> None:
    assert _classify(operational_error=True, operational_subclass=True)
    assert not _classify(operational_subclass=True)


def test_a_connection_exception_class_is_transient() -> None:
    assert _classify(operational_error=True, operational_subclass=True, sqlstate="08006")


# meiva:note
# The falsifier: a child interpreter that refuses psycopg imports the policy,
# answers one classification, and reports the adapter unimportable.
def test_the_policy_module_answers_with_the_driver_blocked() -> None:
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-I", "-B", str(_PROBE), str(_REPO_ROOT)],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr
    assert "driver confined" in result.stdout
