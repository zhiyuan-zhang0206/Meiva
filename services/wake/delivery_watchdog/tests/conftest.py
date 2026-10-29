from collections.abc import Iterator
from pathlib import Path

import pytest

from base import telemetry
from base.cluster.machine import MachineIdentityReader
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.tests.config_authority import config_authority as config_authority
from base.db import Database
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths import meiva_home
from base.paths.tests.homes import unit_home as unit_home
from meiva.tests.fixtures.sdk import sdk_environment as sdk_environment
from meiva.tests.fixtures.sdk import sdk_identity as sdk_identity
from meiva.tests.fixtures.sdk import sdk_metering as sdk_metering


@pytest.fixture
def event_pipeline(
    database: Database, machine_identity: MachineIdentityReader, unit_home: Path
) -> Iterator[telemetry.EventPipeline]:
    """Own the process writer a delivered inbound and a service operation share."""
    pipeline = telemetry.build_pipeline(
        database=lambda: database, machine=machine_identity, home=unit_home
    )
    try:
        telemetry.init_telemetry(
            home=meiva_home(),
            process="test-wake-service",
            pipeline=pipeline,
            machine_reader=machine_identity.name,
        )
        yield pipeline
    finally:
        pipeline.stop(timeout=2)
