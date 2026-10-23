"""A runner's bootstrap fetch against the live gateway endpoint."""

from typing import Any
from urllib.parse import urlsplit

import pytest

from base import config
from base.cluster.machine import MachineIdentity
from base.cluster.machine.tests.identity import machine_identity as machine_identity
from base.config.domains.storage.data_plane import self_machine_host
from base.host.env import bootstrap
from base.host.env.boot_policy import BootstrapFetch
from base.host.net.predicates import is_loopback_host
from base.host.net.tests.local_http import serve_asgi
from base.host.net.url_secret import url_with_host
from gateway.tests.auth.gateway_config import gateway_test_client

# meiva:note
# An isolated test credential, never a real secret.
_SECRET = "live-secret"  # noqa: S105


def test_fetch_bootstrap_config_against_live_endpoint(
    db_conn,
    monkeypatch: pytest.MonkeyPatch,
    served_gateway_home: Any,
    *,
    machine_identity: MachineIdentity,
) -> None:
    # An authenticated gateway: the runner presents its delivered machine API
    # token (the active generation's runner token), never the human secret.
    # meiva:note
    # The runner dials the assembled gateway app through a real loopback listener.
    with (
        gateway_test_client(machine=machine_identity, cluster_secret=_SECRET) as client,
        serve_asgi(client.app) as url,
    ):
        values = bootstrap.fetch_bootstrap_config(
            url, bearer=served_gateway_home.api.runner, tuning=BootstrapFetch()
        )
    # Bootstrap serves the credential-free endpoint even though this gateway
    # keeps an active write generation: its runner login never travels here. A
    # multi-host gateway also rewrites its loopback host to the reachable
    # address for remote runners.
    runner = served_gateway_home.roles.runner
    expected = str(config.settings.data_plane.db_url)
    reachable = self_machine_host()
    if not is_loopback_host(reachable):
        expected = url_with_host(expected, reachable)
    actual_parts = urlsplit(values["MEIVA_DB_URL"])
    expected_parts = urlsplit(expected)
    # libpq dial hints such as hostaddr are implementation-specific query
    # parameters. The endpoint's connection identity must still match.
    assert (
        actual_parts.scheme,
        actual_parts.username,
        actual_parts.password,
        actual_parts.hostname,
        actual_parts.port,
        actual_parts.path.lstrip("/"),
    ) == (
        expected_parts.scheme,
        expected_parts.username,
        None,
        expected_parts.hostname,
        expected_parts.port,
        expected_parts.path.lstrip("/"),
    )
    assert runner.password not in "".join(values.values())
    # Nor does it serve the human secret: a remote unit never holds it.
    assert "MEIVA_CLUSTER_SECRET" not in values
    assert "test-cluster-secret" not in "".join(values.values())
