from __future__ import annotations

import pytest

from base.cluster.machine import GatewayAccess, gateway_auth_headers
from base.host.net.tests.local_http import serve_json


def _access(gateway_url: str, secret: str) -> GatewayAccess:
    return GatewayAccess(
        gateway_url=gateway_url,
        cluster_secret=secret,
        remote_plane=False,
        machine_token=None,
        launcher_profile=None,
        gateway_home=True,
    )


def test_cmd_status_gateway_cluster_serves_line_shows_station(
    capsys: pytest.CaptureFixture[str],
) -> None:
    from cli.commands.lifecycle.status import print_gateway_cluster_status

    with serve_json(
        {
            "machine_name": "station-a",
            "serve_gateway": False,
            "serve_agent_runner": False,
            "serve_observability_station": True,
            "paused": False,
        }
    ) as served:
        print_gateway_cluster_status(_access(served.url, "s3cr3t"))
    out = capsys.readouterr().out
    assert "machine_name: station-a" in out
    assert "serves:       observability-station" in out


def test_fetch_gateway_cluster_status_sends_bearer() -> None:
    # meiva:note
    # The `meiva status` cluster-status probe presents this process's gateway credential, so an
    # authenticated-but-healthy gateway reads as up instead of a false 401. Which credential
    # wins — a delivered machine token or the cluster secret — is base.cluster.machine's contract.
    # meiva:link base/cluster/machine/docs/machine.meiva.okf.md
    from cli.commands.cluster.control import fetch_gateway_cluster_status

    with serve_json({"ok": True}) as served:
        access = _access(served.url, "s3cr3t")
        assert fetch_gateway_cluster_status(access) == {"ok": True}
    (request,) = served.requests
    assert request.path == "/api/cluster/status"
    for name, value in gateway_auth_headers(access).items():
        assert request.headers[name.lower()] == value


def test_fetch_gateway_cluster_status_without_a_credential_raises_instead_of_dialing() -> None:
    # meiva:note
    # The API always authenticates: with neither a token nor a secret the call is named, not sent
    # as a blank bearer. The credential precedence itself is base.cluster.machine's contract.
    # meiva:link base/cluster/machine/docs/machine.meiva.okf.md
    from base.cluster.machine import GatewayApiTokenMissing
    from cli.commands.cluster.control import fetch_gateway_cluster_status

    with serve_json({"ok": True}) as served, pytest.raises(GatewayApiTokenMissing):
        fetch_gateway_cluster_status(_access(served.url, ""))
    assert served.requests == [], "an unauthenticated request must not be sent"
