# meiva:note
# The observability values a command reads from its configuration owner: they are plain frozen
# values built once at the boundary, the secrets are read out of their wrapper there and never
# reach a repr, and importing the readers builds no eager configuration.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import SecretStr

from cli.commands.observability.inputs import (
    lgtm_settings_from_boot,
    observability_config_from_boot,
)
from cli.commands.observability.plans.observability_config import (
    DataPlaneInputs,
    GrafanaCredentials,
)
from cli.commands.observability.url_split import split_url
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime

# meiva:note
# Fixture values, not credentials: each one is searched for in a repr.
PASSWORD = "grafana-admin-pw"  # noqa: S105
BOT_TOKEN = "123456:bot-token-value"  # noqa: S105
CLUSTER_SECRET = "cluster-secret-value"  # noqa: S105
REDIS_PASSWORD = "redis-admin-pw"  # noqa: S105
LOGIN = "login-in-the-url"


# meiva:note
# The settings models are never named by the readers, so importing them builds nothing eager: the
# commands that must run while the eager configuration cannot be built import this module through
# the converge step table.
def test_importing_the_readers_does_not_build_the_eager_configuration(tmp_path: Path) -> None:
    child = Path(__file__).with_name("import_probe_child.py")
    subprocess.run(
        [sys.executable, "-I", "-B", str(child)],
        check=True,
        env={**os.environ, "MEIVA_HOME": str(tmp_path), "MEIVA_CONFIG_FETCH": "skip"},
    )


def test_the_native_lgtm_settings_are_read_from_one_owner(command_runtime: CommandRuntime) -> None:
    config = command_runtime.config
    config.set_field("grafana_admin_password", SecretStr(PASSWORD))
    config.set_field("telegram_bot_token", BOT_TOKEN)
    config.set_field("telegram_owner_id", 42)
    config.set_field("gateway_url", "https://gateway.example")
    config.set_field("gateway_port", 8123)
    config.set_field("lgtm_native_download_attempts", 7)

    settings = lgtm_settings_from_boot(config)

    assert settings.observability == observability_config_from_boot(config)
    assert settings.webhook.gateway_url == "https://gateway.example"
    assert settings.webhook.gateway_port == 8123
    assert settings.credentials == GrafanaCredentials(PASSWORD, BOT_TOKEN, 42)
    assert settings.download.attempts == 7


def test_the_grafana_password_is_read_out_of_its_secret_wrapper_at_the_boundary(
    command_runtime: CommandRuntime,
) -> None:
    config = command_runtime.config
    config.set_field("grafana_admin_password", None)
    assert lgtm_settings_from_boot(config).credentials.admin_password is None

    config.set_field("grafana_admin_password", SecretStr(PASSWORD))

    assert lgtm_settings_from_boot(config).credentials.admin_password == PASSWORD


def test_no_secret_of_the_settings_reaches_a_repr() -> None:
    credentials = GrafanaCredentials(PASSWORD, BOT_TOKEN, 42)
    plane = DataPlaneInputs(
        db_url=f"postgresql://{LOGIN}@127.0.0.1:5433/meiva",
        redis_url=f"redis://{LOGIN}@127.0.0.1:6380/0",
        redis_admin_password=REDIS_PASSWORD,
        is_remote=False,
        cluster_secret=CLUSTER_SECRET,
    )
    url = split_url(f"redis://{LOGIN}:{REDIS_PASSWORD}@127.0.0.1:6380/0")

    rendered = repr(credentials) + repr(plane) + repr(url)

    for secret in (PASSWORD, BOT_TOKEN, CLUSTER_SECRET, REDIS_PASSWORD, LOGIN):
        assert secret not in rendered


def test_a_url_with_a_port_that_is_not_a_number_is_refused_where_it_is_split() -> None:
    with pytest.raises(ValueError, match="Port could not be cast"):
        split_url("http://10.0.0.5:not-a-port")
