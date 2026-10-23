import os
from typing import Any

import psycopg
import pytest

from base.cluster.authority.tests.runner_logins import grant_runner_login
from base.config import settings
from base.db.test_db_guard import assert_test_db_url


# meiva:note
# Real exec children build their environment from the live os.environ, so the runner database
# projection is installed there through the raw-env seam; setup authority stays with the test
# connection. The password belongs to a private test database only.
# meiva:link agent/graph/exec/docs/subprocess-tests.meiva.okf.md
@pytest.fixture
def runner_exec_env(db_conn: psycopg.Connection[Any], monkeypatch: pytest.MonkeyPatch) -> None:
    url = settings.data_plane.db_url
    assert_test_db_url(url, context="real agent exec fixture")
    password = "impersonation-test-runner-password"  # noqa: S105
    runner_url = grant_runner_login(
        url, owner="meiva_citest", login="meiva_g0_runner", password=password
    )
    monkeypatch.setitem(os.environ, "MEIVA_DB_URL", runner_url)
    assert db_conn.info.user != "meiva_g0_runner"
