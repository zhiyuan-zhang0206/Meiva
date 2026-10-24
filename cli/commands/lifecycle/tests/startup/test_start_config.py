# meiva:note
# The configuration values one start reads are the invocation's own, read when the start asks.

from __future__ import annotations

from cli.commands.lifecycle.start.start_config import read_start_config
from cli.database.runtime import CommandRuntime


def test_the_start_reads_the_invocations_configuration_not_the_process(
    command_runtime: CommandRuntime,
) -> None:
    config = command_runtime.config
    config.set_field("gateway_url", "http://gateway.example:8123")
    other = read_start_config(config)

    config.set_field("gateway_url", "http://elsewhere.example:9")
    changed = read_start_config(config)

    assert other.gateway_url == "http://gateway.example:8123"
    assert changed.gateway_url == "http://elsewhere.example:9"


def test_every_field_is_the_configurations_value(command_runtime: CommandRuntime) -> None:
    view = command_runtime.config.view

    config = read_start_config(command_runtime.config)

    assert config.service_path == view.general.service_path
    assert config.gateway_port == view.gateway.gateway_port
    assert config.browser_origin == view.gateway.browser_origin
    assert config.entry_url == view.services.frontend_healthcheck_url
    assert config.app_port == view.services.app_port
    assert config.browser_cdp_port == view.services.browser_cdp_port
    assert config.walg_config_file == view.walg.walg_config_file
    assert config.data_plane.db_url == view.data_plane.db_url
