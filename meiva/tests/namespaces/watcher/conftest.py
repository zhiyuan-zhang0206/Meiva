"""Consumer-local opt-in fixtures for this test directory."""

import pathlib

import pytest

from base.config.tests.config_authority import config_authority as config_authority
from base.packages.tests.model_catalog import add_bindings as add_bindings
from base.packages.tests.model_catalog import add_models as add_models
from base.packages.tests.model_catalog import model_catalog as model_catalog
from base.packages.tests.model_catalog import set_prices as set_prices
from meiva.tests.fixtures.model_owner import model_installation as model_installation
from meiva.tests.fixtures.model_owner import sdk_model_owner as sdk_model_owner
from meiva.watcher import wiring
from meiva.watcher.spawn import SpawnDeps


@pytest.fixture
def spawn_deps() -> SpawnDeps:
    """The spawn deps of the bound agent, read now: its real sessions, directories and CLI."""
    return wiring.spawn_deps()


@pytest.fixture
def fake_cli(tmp_path: pathlib.Path) -> pathlib.Path:
    """A CLI that records its argv to `argv.txt` beside it, so no gateway is needed."""
    argv_file = tmp_path / "argv.txt"
    cli = tmp_path / "fake-meiva"
    cli.write_text(f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {argv_file}\n")
    cli.chmod(0o755)
    return cli
