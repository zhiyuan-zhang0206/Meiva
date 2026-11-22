import subprocess

import pytest

from base.clock.tests.fakes import system_clock
from base.host.permissions_helper.client import PermissionsHelperError
from base.host.remote_hosts import RemoteHostConfig
from services.desktop.remote.ssh_tunnel import SshTunnelManager


@pytest.mark.parametrize("ready", [False, True])
def test_browser_tunnel_targets_cdp_reuses_and_reaps(*, ready: bool) -> None:
    launches: list[tuple[list[str], subprocess.Popen[bytes]]] = []

    def launch(argv: list[str]) -> subprocess.Popen[bytes]:
        proc = subprocess.Popen(
            ["sleep", "30"],
            stderr=subprocess.PIPE,
        )
        launches.append((argv, proc))
        return proc

    manager = SshTunnelManager(
        timers=system_clock(),
        startup_timeout_s=0.1,
        launch=launch,
        port_accepting=lambda _port: ready,
    )
    config = RemoteHostConfig(
        "test-pc", "windows", "ssh-fixture", local_port=19022, browser_port=9333
    )
    try:
        if ready:
            endpoint = manager.endpoint_for(config, browser=True)
            assert endpoint.startswith("http://127.0.0.1:")
            assert endpoint != "http://127.0.0.1:19022"
            assert endpoint == manager.endpoint_for(config, browser=True)
        else:
            with pytest.raises(PermissionsHelperError, match="did not become ready"):
                manager.endpoint_for(config, browser=True)
            assert launches[0][1].poll() is not None
        assert len(launches) == 1
        argv, _ = launches[0]
        assert argv[argv.index("-L") + 1].startswith("127.0.0.1:")
        assert argv[argv.index("-L") + 1].endswith(":127.0.0.1:9333")
        assert argv[argv.index("-S") + 1] == "none"
        assert "StrictHostKeyChecking=yes" in argv
        assert argv[-2:] == ["--", "ssh-fixture"]
    finally:
        manager.close_all()
    assert launches[0][1].poll() is not None
