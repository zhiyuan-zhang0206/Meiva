# meiva:note
# Fresh source imports and private file writes; scheduler commands use an existing resource.

import json
import os
import plistlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[5]))


def _cold() -> None:
    from base.host.system.cron import health_content

    assert Path(health_content.__file__).resolve() == (
        Path(__file__).resolve().parents[2] / "cron/health_content.py"
    )
    for name in (
        "base.host.system.cron.mechanics",
        "base.host.system.cron.health_job",
        "base.host.system.hostport",
        "base.host.system.boot_policy",
    ):
        assert name not in sys.modules, name


def _verify(plist: Path) -> None:
    values = plistlib.loads(plist.read_bytes())
    assert values["Label"] == "com.meiva.health-probe"
    assert values["ProgramArguments"] == [
        str(Path(__file__).resolve().parents[5] / ".venv/bin/meiva"),
        "cluster",
        "health-probe",
    ]
    assert values["RunAtLoad"] is False
    assert values["StartInterval"] == 300
    assert values["EnvironmentVariables"]["MEIVA_HOME"] == str(Path.home() / ".meiva")
    assert "/safe/bin" in values["EnvironmentVariables"]["PATH"]
    assert values["StandardOutPath"] == str(Path.home() / ".meiva/logs/health-probe.log")
    assert values["StandardErrorPath"] == values["StandardOutPath"]


def _roots():
    from base.host.system.job_values import JobRoots

    return JobRoots(
        home=Path.home() / ".meiva",
        repo=Path(__file__).resolve().parents[5],
        user_home=Path.home(),
    )


def _external(*, fails: bool) -> None:
    from base.host.system.cron import health_job
    from base.host.system.tests.fake_commands import DARWIN_ARM64, FakeCommands, Reply, fake_host

    commands = FakeCommands()
    commands.on(["launchctl", "bootout"], Reply(returncode=113), repeat=True)
    commands.on(
        ["launchctl", "bootstrap"],
        Reply(returncode=1 if fails else 0, stderr="refused"),
        repeat=True,
    )
    # meiva:note
    # The host carries the environment this child was launched with: `XPC_SERVICE_NAME` is one key
    # of it, and the registrar reads the label from the host, not from the live process.
    host = fake_host(
        commands,
        platform=DARWIN_ARM64,
        programs={"brew": "/safe/bin/brew"},
        uid=501,
        environ=dict(os.environ),
    )
    plist = Path.home() / "Library/LaunchAgents/com.meiva.health-probe.plist"
    assert health_job.register_macos(300, host=host, roots=_roots()) == (1 if fails else 0)
    _verify(plist)
    assert commands.argvs == [
        ("launchctl", "bootout", "gui/501/com.meiva.health-probe"),
        ("launchctl", "bootstrap", "gui/501", str(plist)),
    ]
    if not fails:
        os.utime(plist, ns=(1_000_000_000, 1_000_000_000))
        previous = plist.stat().st_mtime_ns
        assert health_job.register_macos(300, host=host, roots=_roots()) == 0
        assert plist.stat().st_mtime_ns != previous
        assert [argv[1] for argv in commands.argvs] == ["bootout", "bootstrap"] * 2
        _verify(plist)
    assert all(call.timeout == 30.0 for call in commands.calls)


def _deferred() -> None:
    from base.host.system.cron import health_job
    from base.host.system.tests.fake_commands import DARWIN_ARM64, FakeCommands, fake_host

    commands = FakeCommands()
    host = fake_host(commands, platform=DARWIN_ARM64, environ=dict(os.environ))
    assert health_job.register_macos(300, host=host, roots=_roots()) == 0
    assert not (Path.home() / "Library").exists()
    assert not (Path.home() / ".meiva").exists()
    assert commands.argvs == []


# meiva:note
# This fixed repository child records the rendered command suffix and its actual environment.
def _record() -> None:
    print(json.dumps({"argv": sys.argv[2:], "home": os.environ["MEIVA_HOME"]}))  # noqa: T201


match sys.argv[1]:
    case "record":
        _record()
    case "cold":
        _cold()
    case "external":
        _external(fails=False)
    case "failure":
        _external(fails=True)
    case "deferred":
        _deferred()
    case other:
        raise ValueError(other)
