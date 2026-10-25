from __future__ import annotations

import os
from collections.abc import Iterator

import pytest

from base.db.test_db_guard import assert_test_db_url


@pytest.fixture(autouse=True)
def _restore_test_db_env() -> Iterator[None]:
    saved = os.environ.get("MEIVA_TEST_DB")
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("MEIVA_TEST_DB", None)
        else:
            os.environ["MEIVA_TEST_DB"] = saved


def test_main_cluster_db_always_refused() -> None:
    os.environ.pop("MEIVA_TEST_DB", None)
    with pytest.raises(RuntimeError, match="production database"):
        assert_test_db_url(
            "postgresql://meiva_main:***@10.0.0.2:6433/meiva_main",
            context="test",
        )
    os.environ["MEIVA_TEST_DB"] = "1"
    with pytest.raises(RuntimeError, match="production database"):
        assert_test_db_url("postgresql://meiva_main@127.0.0.1:6433/meiva_main", context="test")


def test_main_cluster_hostname_always_refused() -> None:
    os.environ["MEIVA_TEST_DB"] = "1"
    with pytest.raises(RuntimeError, match="production database"):
        assert_test_db_url(
            "postgresql://x@meiva_main-gw.example.internal:6433/somedb",
            context="test",
        )


def test_fresh_birth_name_needs_marker() -> None:
    os.environ.pop("MEIVA_TEST_DB", None)
    with pytest.raises(RuntimeError, match="MEIVA_TEST_DB"):
        assert_test_db_url("postgresql://meiva@127.0.0.1:5433/meiva", context="test")
    os.environ["MEIVA_TEST_DB"] = "1"
    assert assert_test_db_url("postgresql://meiva@127.0.0.1:5433/meiva", context="test") is None


def test_throwaway_names_allowed() -> None:
    os.environ.pop("MEIVA_TEST_DB", None)
    assert (
        assert_test_db_url("postgresql://meiva@127.0.0.1:54321/meiva_citest", context="test")
        is None
    )
    assert (
        assert_test_db_url("postgresql://meiva@127.0.0.1:54322/meiva_test_42", context="test")
        is None
    )


def test_unprovisioned_sentinel_allowed() -> None:
    os.environ.pop("MEIVA_TEST_DB", None)
    assert (
        assert_test_db_url("postgresql://unprovisioned@127.0.0.1:1/unprovisioned", context="test")
        is None
    )


def test_unknown_name_needs_marker() -> None:
    os.environ.pop("MEIVA_TEST_DB", None)
    with pytest.raises(RuntimeError, match="MEIVA_TEST_DB"):
        assert_test_db_url("postgresql://u@127.0.0.1:5433/mydb", context="test")
    os.environ["MEIVA_TEST_DB"] = "1"
    assert assert_test_db_url("postgresql://u@127.0.0.1:5433/mydb", context="test") is None


def test_missing_url_refused() -> None:
    with pytest.raises(RuntimeError, match="not set"):
        assert_test_db_url(None, context="test")


def test_error_carries_context() -> None:
    with pytest.raises(RuntimeError, match="my-seed-helper"):
        assert_test_db_url(
            "postgresql://meiva_main@127.0.0.1:6433/meiva_main",
            context="my-seed-helper",
        )
