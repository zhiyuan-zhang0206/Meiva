from __future__ import annotations

import pytest

from base.config import settings
from cli.commands.cluster import control
from cli.database.tests.command_fakes import fake_session_backends as fake_session_backends
from cli.database.tests.command_fakes import gateway_role_pinned as gateway_role_pinned
from cli.database.tests.command_fakes import hermetic_gateway_base as hermetic_gateway_base
from cli.database.tests.command_inputs import command_runtime as command_runtime


def test_cmd_status_gateway_cluster_serves_line_shows_station(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        control,
        "fetch_gateway_cluster_status",
        lambda: {
            "machine_name": "station-a",
            "serve_gateway": False,
            "serve_agent_runner": False,
            "serve_observability_station": True,
            "paused": False,
        },
    )
    from cli.commands.lifecycle.status import print_gateway_cluster_status

    print_gateway_cluster_status()
    out = capsys.readouterr().out
    assert "machine_name: station-a" in out
    assert "serves:       observability-station" in out


def test_fetch_gateway_cluster_status_sends_bearer(monkeypatch: pytest.MonkeyPatch) -> None:
    # meiva:note
    # The `meiva status` cluster-status probe presents this process's gateway credential, so an
    # authenticated-but-healthy gateway reads as up instead of a false 401. Which credential
    # wins — a delivered machine token or the cluster secret — is base.cluster.machine's contract.
    # meiva:link base/cluster/machine/docs/machine.meiva.okf.md
    import httpx

    from base.cluster.machine import gateway_auth_headers
    from base.cluster.machine.ambient import gateway_access_from_settings
    from cli.commands.cluster.control import fetch_gateway_cluster_status

    monkeypatch.setattr(settings.data_plane, "cluster_secret", "s3cr3t")
    captured: dict[str, object] = {}

    class _Resp:
        def raise_for_status(self) -> None: ...

        def json(self) -> dict[str, object]:
            return {"ok": True}

    def _fake_get(url: str, *, timeout: float, headers: dict[str, str]) -> _Resp:
        captured["url"] = url
        captured["headers"] = headers
        return _Resp()

    monkeypatch.setattr(httpx, "get", _fake_get)
    assert fetch_gateway_cluster_status() == {"ok": True}
    assert captured["url"] == "http://gw:8000/api/cluster/status"
    assert captured["headers"] == gateway_auth_headers(gateway_access_from_settings())


def test_fetch_gateway_cluster_status_without_a_credential_raises_instead_of_dialing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # meiva:note
    # The API always authenticates: with neither a token nor a secret the call is named, not sent
    # as a blank bearer. The credential precedence itself is base.cluster.machine's contract.
    # meiva:link base/cluster/machine/docs/machine.meiva.okf.md
    import httpx

    from base.cluster.machine import GatewayApiTokenMissing
    from cli.commands.cluster.control import fetch_gateway_cluster_status

    monkeypatch.delenv("MEIVA_API_TOKEN", raising=False)
    monkeypatch.setattr(settings.data_plane, "cluster_secret", "")

    def _no_dial(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("an unauthenticated request must not be sent")

    monkeypatch.setattr(httpx, "get", _no_dial)
    with pytest.raises(GatewayApiTokenMissing):
        fetch_gateway_cluster_status()
