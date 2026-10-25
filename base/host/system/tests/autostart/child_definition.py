# meiva:note
# Fresh-process definition and private-file scenarios; never contacts the OS scheduler.

from __future__ import annotations

import os
import plistlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))


def _cold() -> None:
    from base.host.system.boot_autostart import content

    assert Path(content.__file__).resolve() == (
        Path(__file__).resolve().parents[2] / "boot_autostart/content.py"
    )
    for name in (
        "base.host.system.hostport",
        "base.host.system.cron",
        "base.host.system.boot_policy",
        "base.host.system.boot_unit",
    ):
        assert name not in sys.modules, name


def _assert_written(plist: Path) -> None:
    from base.host.system.boot_policy import BOOT_RETRY_INTERVAL_S

    values = plistlib.loads(plist.read_bytes())
    assert values["ProgramArguments"] == [
        str(Path(__file__).resolve().parents[5] / ".venv/bin/meiva"),
        "start",
        "--boot",
        "--only-service",
        "frontend",
        "--only-service",
        "gateway",
    ]
    assert values["EnvironmentVariables"]["MEIVA_HOME"] == str(Path.home() / ".meiva")
    assert "/safe/bin" in values["EnvironmentVariables"]["PATH"]
    assert values["StandardOutPath"] == str(Path.home() / ".meiva/logs/autostart.log")
    assert values["StandardErrorPath"] == values["StandardOutPath"]
    assert values["RunAtLoad"] is True
    assert values["KeepAlive"] == {"SuccessfulExit": False}
    assert values["ThrottleInterval"] == BOOT_RETRY_INTERVAL_S


def _registration(scenario: str) -> None:
    from base.host.system.boot_autostart import registration
    from base.host.system.tests.fake_commands import DARWIN_ARM64, FakeCommands, fake_host

    # meiva:note
    # Any actual scheduler command is an assertion failure.
    commands = FakeCommands()
    host = fake_host(commands, platform=DARWIN_ARM64, programs={"brew": "/safe/bin/brew"})
    plist = Path.home() / "Library/LaunchAgents/com.meiva.autostart.plist"

    def _unchanged() -> None:
        os.utime(plist, ns=(1_000_000_000, 1_000_000_000))
        before = plist.stat()
        assert (
            registration.register_macos(services=frozenset({"frontend", "gateway"}), host=host) == 0
        )
        after = plist.stat()
        assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
        assert registration.register_macos(services=frozenset({"gateway"}), host=host) == 0
        assert plistlib.loads(plist.read_bytes())["ProgramArguments"][-2:] == [
            "--only-service",
            "gateway",
        ]

    if scenario == "empty":
        plist.parent.mkdir(parents=True)
        plist.write_text("registered")
        try:
            registration.register_autostart(services=frozenset(), enabled=True, host=host)
        except ValueError as exc:
            assert "starts no service" in str(exc)
        else:
            raise AssertionError("an empty boot selection was accepted")
        assert plist.read_text() == "registered"
    else:
        assert (
            registration.register_macos(services=frozenset({"gateway", "frontend"}), host=host) == 0
        )
        assert plist.exists()
        _assert_written(plist)
        if scenario == "idempotent":
            _unchanged()
        elif scenario != "write":
            raise ValueError(scenario)
    assert commands.argvs == []


if sys.argv[1] == "cold":
    _cold()
else:
    _registration(sys.argv[1])
