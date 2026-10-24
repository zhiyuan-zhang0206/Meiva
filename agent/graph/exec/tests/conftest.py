from datetime import UTC, datetime
from pathlib import Path

import pytest

from agent.graph.exec._crop import CropLimits
from agent.graph.exec.output import OutputLimits
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.tests.config_authority import config_authority as config_authority
from base.events.live.tests.live_capture import live_events as live_events
from base.log.tests.log_records import loguru_records as loguru_records
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from base.paths.tests.homes import unit_home as unit_home
from meiva.tests.fixtures.sdk import sdk_environment as sdk_environment
from meiva.tests.fixtures.sdk import sdk_identity as sdk_identity
from meiva.tests.fixtures.sdk import sdk_metering as sdk_metering


@pytest.fixture
def limits(tmp_path: Path) -> OutputLimits:
    return OutputLimits(
        archive_dir=tmp_path / ".exec_output",
        crop=CropLimits(
            exec_output_crop_after_lines=300,
            exec_output_crop_after_chars=65536,
            exec_output_crop_after_bytes=65536,
            exec_output_crop_head_lines=25,
            exec_output_crop_tail_lines=25,
            exec_output_crop_archive_max_bytes=16777216,
        ),
        timeout_seconds=60,
        max_chars=30_000,
    )


@pytest.fixture
def output_now() -> datetime:
    return datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
