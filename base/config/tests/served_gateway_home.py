"""A gateway home that serves the test authority's data plane and an active generation."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from base.config.service_read import ConfigAuthority


@pytest.fixture
def served_gateway_home(
    config_authority: ConfigAuthority,
    seed_write_generation: Callable[[Path], Any],
) -> Any:
    """Serve the test authority's data plane and active generation from one home.

    Build the file from the isolated boot model, never another ambient home.
    Return the generation's secret record as before.
    """
    home = config_authority.env_path.parent
    config_authority.env_path.write_text(
        f"MEIVA_DB_URL={config_authority.service_field_value('db_url')}\n"
        f"MEIVA_REDIS_URL={config_authority.service_field_value('redis_url')}\n"
    )
    return seed_write_generation(home)
