from typing import Any

import psycopg
import pytest

from base.cluster.authority.tests.runner_logins import grant_runner_login
from base.config import settings
from base.db.test_db_guard import assert_test_db_url


# meiva:note
# The runner login's database URL, as a value: real exec children connect as the runner role,
# so the test hands this to the child's environment (`ParentFacts.environ`) instead of the
# owner login. Setup authority stays with the test connection. The password belongs to a
# private test database only.
# meiva:link agent/graph/exec/docs/subprocess-tests.meiva.okf.md
@pytest.fixture
def runner_db_url(db_conn: psycopg.Connection[Any]) -> str:
    url = settings.data_plane.db_url
    assert_test_db_url(url, context="real agent exec fixture")
    password = "impersonation-test-runner-password"  # noqa: S105
    runner_url = grant_runner_login(
        url, owner="meiva_citest", login="meiva_g0_runner", password=password
    )
    assert db_conn.info.user != "meiva_g0_runner"
    return runner_url
