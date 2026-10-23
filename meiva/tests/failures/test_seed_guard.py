from __future__ import annotations

import pytest

from meiva.tests.fixtures import meiva_tests as conftest


# meiva:note
# The seeding helper refuses a production database URL before it opens any connection: the
# refusal is the guard's RuntimeError, where a connection attempt to that address would fail
# with the driver's own error instead.
@pytest.mark.parametrize(
    "db_url",
    [
        "postgresql://meiva_main:***@10.0.0.2:6433/meiva_main",
        "postgresql://meiva_main@127.0.0.1:6433/meiva_main",
    ],
)
def test_ensure_agents_meta_row_refuses_prod_db(db_url: str) -> None:
    with pytest.raises(RuntimeError, match="production database"):
        conftest.ensure_agents_meta_row(900_000, db_url=db_url)
