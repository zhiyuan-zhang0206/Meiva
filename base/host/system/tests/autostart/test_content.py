# meiva:note
# The selected boot command and retry definition are values, not host observations.

import plistlib
import re
from pathlib import Path

from base.host.system.boot_autostart.content import launchd_plist_content
from base.host.system.boot_policy import BOOT_RETRY_INTERVAL_S, boot_start_arguments

_SERVICES = frozenset({"gateway", "frontend"})


def _definition() -> str:
    return launchd_plist_content(
        arguments=("/Users/x/.local/bin/meiva", *boot_start_arguments(_SERVICES)),
        retry_interval_seconds=BOOT_RETRY_INTERVAL_S,
        log_file=Path("/private/home/.meiva/logs/autostart.log"),
        environment_home=Path("/private/home/.meiva"),
        path_env="/opt/homebrew/bin:/usr/bin",
    )


def test_plist_runs_meiva_start_at_load() -> None:
    xml = _definition()
    assert "<string>com.meiva.autostart</string>" in xml
    assert "<key>RunAtLoad</key>" in xml and "<true/>" in xml
    # meiva:note
    # ProgramArguments == `meiva start --boot` — no name flag exists (path-only).
    for token in ("/Users/x/.local/bin/meiva", "<string>start</string>", "<string>--boot</string>"):
        assert token in xml
    assert "--cluster" not in xml


def test_plist_selects_every_service_by_name_sorted() -> None:
    xml = _definition()
    arguments = xml.split("<key>ProgramArguments</key>", 1)[1].split("</array>", 1)[0]
    assert re.findall(r"<string>(.*?)</string>", arguments) == [
        "/Users/x/.local/bin/meiva",
        "start",
        "--boot",
        "--only-service",
        "frontend",
        "--only-service",
        "gateway",
    ]


def test_plist_embeds_a_path_env() -> None:
    # meiva:note
    # The plist must carry an explicit PATH — launchd's minimal PATH omits
    # Homebrew's bin, which is what made a bare `meiva start` fail to find its tools at
    # boot. The PATH composition itself is `base.host.system.cron.mechanics.launchd_path_env`
    # (shared with the watchdog probe's plist) and is unit-tested there.
    xml = _definition()
    assert "<key>PATH</key>" in xml
    assert "/opt/homebrew/bin" in xml


def test_the_job_retries_on_macos_only_while_it_fails() -> None:
    # meiva:note
    # launchd.plist(5): `SuccessfulExit: false` restarts the job "in the
    # inverse condition" of a zero exit -- i.e. while it keeps failing, and never
    # once it succeeds. Getting this key backwards (or passing a bare
    # `KeepAlive: true`) would respawn `meiva start` forever, including after a
    # deliberate `meiva stop`.
    xml = _definition()
    assert "<key>KeepAlive</key>" in xml
    keep_alive = xml.split("<key>KeepAlive</key>", 1)[1].split("</dict>", 1)[0]
    assert "<key>SuccessfulExit</key>" in keep_alive
    assert "<false/>" in keep_alive
    # meiva:note
    # A true here means respawn after success.
    assert "<true/>" not in keep_alive


def test_the_job_retries_on_macos_at_the_shared_interval() -> None:
    # meiva:note
    # ThrottleInterval overrides launchd's 10s respawn floor; without it a
    # failing start would be retried six times a minute.
    xml = _definition()
    assert "<key>ThrottleInterval</key>" in xml
    assert f"<integer>{BOOT_RETRY_INTERVAL_S}</integer>" in xml


def test_log_and_environment_paths_round_trip_xml_metacharacters(tmp_path: Path) -> None:
    home = tmp_path / "home & <private>"
    log_file = home / "logs" / "autostart & <output>.log"
    binary = str(home / "bin" / "meiva & <entry>")
    path_env = f"{home}/bin:/usr/bin"
    xml = launchd_plist_content(
        arguments=(binary, "start", "--boot", "--only-service", "gateway"),
        retry_interval_seconds=60,
        log_file=log_file,
        environment_home=home,
        path_env=path_env,
    )
    values = plistlib.loads(xml.encode())
    assert values["StandardOutPath"] == str(log_file)
    assert values["StandardErrorPath"] == str(log_file)
    assert values["ProgramArguments"][0] == binary
    assert values["EnvironmentVariables"] == {"MEIVA_HOME": str(home), "PATH": path_env}
