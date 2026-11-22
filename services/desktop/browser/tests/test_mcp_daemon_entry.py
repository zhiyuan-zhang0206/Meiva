from pathlib import Path

from base.config import ConfigBoot
from base.native_process.loaded_commit import LoadedCommit
from services.desktop.browser import mcp_daemon as daemon


def test_entry_keeps_the_config_owner_and_captured_image(tmp_path: Path) -> None:
    boot = ConfigBoot()
    boot.read_process_environment()
    boot.set_field("machine_name", "browser-entry")
    repo = Path(__file__).resolve().parents[4]
    image = LoadedCommit(repo, None)
    home = tmp_path / "home"
    wiring = daemon.build_entry_wiring(config=boot, image=image, home=home)

    assert wiring.process_name == "browser-mcp"
    assert wiring.image is image
    assert wiring.process_home == home.resolve()
    assert wiring.machine.name() == "browser-entry"
    assert wiring.browser_cdp_port == boot.view.services.browser_cdp_port
    assert wiring.gateway_access.gateway_url == boot.view.gateway.gateway_url
    assert wiring.clock.now().utcoffset() is not None

    boot.set_field("mcp_connect_timeout_seconds", 17.0)
    assert wiring.connect_timeout_seconds() == 17.0
