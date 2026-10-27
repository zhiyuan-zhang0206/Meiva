from __future__ import annotations

from pathlib import Path

import pytest

from base.config.tests.configuration import snapshot_process_config
from base.native_process import code_version
from base.native_process.loaded_commit import LoadedCommit
from base.paths import meiva_home
from services.desktop.computer import mcp_daemon as daemon


@pytest.mark.parametrize("has_commit", [True, False])
def test_entry_shares_the_captured_gate_with_logging_and_actions(has_commit: bool) -> None:
    captured = LoadedCommit.capture()
    image = captured if has_commit else LoadedCommit(Path("/entry-computer"), None)
    boot = snapshot_process_config()
    boot.set_field("machine_name", "computer-entry")

    entry = daemon.prepare_entry(image, boot, home=meiva_home())

    assert entry.image is image
    assert entry.machine.name() == "computer-entry"
    assert entry.direct_url.local_host == entry.machine.host
    if not has_commit:
        with pytest.raises(code_version.CodeVersionError, match="loaded commit"):
            entry.gate.application_name()
    else:
        app_name = entry.gate.application_name()
        assert app_name.startswith("meiva:computer-mcp:v")
        assert entry.gate.application_name() == app_name
