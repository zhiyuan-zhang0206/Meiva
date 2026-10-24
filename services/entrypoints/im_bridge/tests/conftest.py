from collections.abc import Iterator
from pathlib import Path

import pytest
from psycopg import Connection
from psycopg_pool import ConnectionPool

from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config import settings
from base.config.tests.config_authority import config_authority as config_authority
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
from meiva.tests.fixtures.sdk import sdk_environment as sdk_environment
from meiva.tests.fixtures.sdk import sdk_identity as sdk_identity
from meiva.tests.fixtures.sdk import sdk_metering as sdk_metering
from services.entrypoints.im_bridge.config import state_dir_of


@pytest.fixture
def pool(db_url: str) -> Iterator[ConnectionPool]:
    with ConnectionPool[Connection](settings.data_plane.db_url, min_size=1, max_size=2) as pool:
        yield pool


@pytest.fixture
def state_dir(tmp_path: Path) -> Path:
    return state_dir_of(tmp_path)
