from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.requires_host("darwin")


def test_pytest_env_carries_lc_all_on_macos() -> None:
    assert os.environ.get("LC_ALL"), (
        "tests/fixtures/env_bootstrap.py must set LC_ALL on macOS — Homebrew PG's postmaster "
        "aborts with 'postmaster became multithreaded during startup' otherwise"
    )
