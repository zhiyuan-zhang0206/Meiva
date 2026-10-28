# meiva:note
# Small scripts that stand in for the coding tools a launch starts, installed where a real session finds them first: the pty-sessions service runs with the test home as its HOME, so a line in the home's `.bash_profile` puts the shim directory ahead of any program the machine has. The scripts are real child processes of a real shell; they render what the real TUI renders and write what they receive to a file the test reads back.
# meiva:link meiva/shell/coding_tools/docs/coding-tools.meiva.okf.md
from __future__ import annotations

import contextlib
import shutil
import sys
import tempfile
from collections.abc import Generator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest

from base.agents.context.clients import ClientSet
from base.agents.context.gateway_client.transport import GatewayTransportInputs
from base.clock import SYSTEM_TIMERS, Timers
from base.paths import workspace_dir
from meiva.shell.coding_tools._common import LaunchSite
from meiva.shell.session_env import SessionHost
from meiva.shell.sessions import ShellSessions

CLAUDE_PANEL = (
    " ▐▛███▜▌   Claude Code v2.1.283\n"
    "  ~/workspace\n"
    "\u276f\n"
    "  bypass permissions on (shift+tab to cycle)\n"
)

CODEX_SESSION = "01a0e1ac-adc7-7d33-bd13-8ce2c6a686c5"


# meiva:note
# The launch site a test runs a launcher with: the agent's real shell sessions, `home` as the home whose Claude and Codex files a launch touches, the agent's workspace as the directory a new session starts in, `host` (`live_host`) so the session finds the programs installed under `home`, and the clock the launch waits against. These launchers run real child processes, so the site defaults to the process's own timers and the instant the site is built; a test that drives a wait passes its own.
def site_for(
    shell: ShellSessions,
    home: Path,
    host: SessionHost,
    *,
    timers: Timers = SYSTEM_TIMERS,
    now: datetime | None = None,
) -> LaunchSite:
    return LaunchSite(
        shell=shell,
        home=home,
        cluster=home / "cluster",
        session_cwd=workspace_dir(shell.agent_id, home=home),
        host=host,
        timers=timers,
        now=datetime.now(UTC) if now is None else now,
    )


# meiva:note
# The SDK's gateway client set for the gateway served at `url`, which is what a bound context
# carries. One attempt per request and no waiting between attempts, so a refusal comes back at
# once.
@contextlib.contextmanager
def gateway_clients(url: str) -> Generator[ClientSet]:
    transport = GatewayTransportInputs(
        max_retries_reader=lambda: 1,
        retry_delay_reader=lambda: 0.0,
        memory_deadline_reader=lambda: 1.0,
    )
    with httpx.Client(base_url=url) as client:
        yield ClientSet(
            gateway_url=url,
            gateway=lambda _url: client,
            factories={GatewayTransportInputs: lambda: transport},
        )


# meiva:note
# A short scratch directory. A launch line is typed into a canonical-mode terminal whose input line holds at most 1024 characters on macOS, and it carries the workspace and marker paths, so a test that runs a launch for real keeps those paths short instead of living under pytest's deep `popen-gwN/test_name_0` directory.
@pytest.fixture
def short_dir() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="cl-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _install(home: Path, name: str, script: str) -> Path:
    shims = home / "shims"
    shims.mkdir(exist_ok=True)
    shim = shims / name
    shim.write_text(script)
    shim.chmod(0o755)
    profile = home / ".bash_profile"
    line = f'export PATH="{shims}:$PATH"\n'
    current = profile.read_text() if profile.exists() else ""
    if line not in current:
        profile.write_text(current + line)
    return shim


# meiva:note
# A `claude` that answers `auth status` with the given sign-in state, renders the Claude Code panel and writes every line it is typed to `record`. `panel_delay` holds the panel back, so the launch has to wait for it.
def install_claude(
    home: Path,
    record: Path,
    *,
    signed_in: bool = True,
    panel: str = CLAUDE_PANEL,
    panel_delay: float = 0.0,
) -> Path:
    return _install(
        home,
        "claude",
        f"""#!/bin/sh
if [ "$1" = auth ]; then exit {0 if signed_in else 1}; fi
sleep {panel_delay}
cat <<'PANEL'
{panel}PANEL
stty -echo 2>/dev/null
cat >> '{record}'
""",
    )


# meiva:note
# A `codex` that is either the shared app server (it binds the `--listen` unix socket and accepts) or the TUI (it writes its arguments to `record` with the suffix `.argv`, shows MCP startup, then the ready banner, answers `/status` with a card carrying `session`, writes every other line it is typed to `record` and shows "Working").
def install_codex(
    home: Path, record: Path, *, session: str = CODEX_SESSION, mcp_seconds: float = 0.3
) -> Path:
    return _install(
        home,
        "codex",
        f"""#!{sys.executable}
import socket
import sys
import time

args = sys.argv[1:]
if args[:1] == ["app-server"]:
    path = args[args.index("--listen") + 1].removeprefix("unix://")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(8)
    while True:
        server.accept()[0].close()
with open({str(record.with_suffix(".argv"))!r}, "a") as out:
    out.write(" ".join(args) + "\\n")
print("Starting MCP servers (0/2)", flush=True)
time.sleep({mcp_seconds})
print("\\x1b[2J\\x1b[H>_ OpenAI Codex (fake) model: gpt directory: /workspace", flush=True)
for line in sys.stdin:
    text = line.strip()
    if text == "/status":
        print("Session:   {session}", flush=True)
        continue
    with open({str(record)!r}, "a") as out:
        out.write(text + "\\n")
    print("Working", flush=True)
""",
    )
