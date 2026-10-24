# meiva:note
# What one watcher launch consists of, decided from values: the request (program, how it ends,
# how it is reported), the three generated files and the command that starts the bootstrap (the
# bootstrap source establishes the agent identity, arms the orphan guard and the optional
# timeout watchdog, and runs the script as `__main__`), and the single shell line the session
# executes. `spawn.run` writes the files and sends the line.
# meiva:link meiva/watcher/docs/launch-line.meiva.okf.md

from __future__ import annotations

import re
import shlex
from collections.abc import Collection, Iterable
from dataclasses import dataclass
from pathlib import Path

from meiva.shell.background_plan import Notice, NotifyPolicy, notified_line

__all__ = [
    "SESSION_ID_ENV",
    "LaunchFiles",
    "SpawnRequest",
    "build_boot",
    "launch_line",
    "plan_files",
    "stale_names",
]

# meiva:note
# A watcher runs as a standalone child process, so it does NOT inherit the agent process's
# in-memory state. The generated bootstrap file establishes identity (agent id inlined) before
# running the watcher script, so every SDK call inside the script - including
# meiva.agents.send_message - knows who launched it. The watcher's own session id rides as an env
# var so the prebuilt time watchers (at / cron) can tag their wake-ups with which watcher fired;
# it names this watcher, not the agent. (Gateway URL / machine auth come from settings; cluster
# env is forwarded onto the session by the session machinery.)
SESSION_ID_ENV = "MEIVA_WATCHER_SESSION_ID"


# meiva:note
# What one watcher is: its program, the watchdog that bounds a launch watcher (None for a
# self-terminating at/cron script), how it is reported, and its session's lifetime in seconds,
# which is the watcher's target deadline (user ruling 2026-09-14, task #3411).
@dataclass(frozen=True)
class SpawnRequest:
    code: str
    watchdog_secs: float | None
    name: str
    ttl_seconds: float
    notify: NotifyPolicy | None = None


# meiva:note
# The generated files of one watcher session and the command that runs the bootstrap: the
# agent's script written verbatim, the bootstrap (identity, watchdog, runpy), and the launch file
# the session sources.
@dataclass(frozen=True)
class LaunchFiles:
    session_id: int
    script_path: Path
    script_text: str
    boot_path: Path
    boot_text: str
    launch_path: Path
    command: str


# meiva:note
# The files for session `session_id` under `watchers_dir`. The script is written verbatim, with
# nothing prepended, so a leading `from __future__` import (which must be the first statement of
# a file) stays valid; the bootstrap lives in its own file.
def plan_files(
    watchers_dir: Path, session_id: int, request: SpawnRequest, agent_id: int, python: str
) -> LaunchFiles:
    script_path = watchers_dir / f"watcher_{session_id}.py"
    boot_path = watchers_dir / f"watcher_{session_id}_boot.py"
    return LaunchFiles(
        session_id=session_id,
        script_path=script_path,
        script_text=request.code,
        boot_path=boot_path,
        boot_text=build_boot(script_path, request.watchdog_secs, agent_id),
        launch_path=watchers_dir / f"watcher_{session_id}_launch.sh",
        command=(
            f"{SESSION_ID_ENV}={session_id} {shlex.quote(python)} {shlex.quote(str(boot_path))}"
        ),
    )


_GENERATED_NAME = re.compile(r"^watcher_(\d+)(?:_boot\.py|_launch\.sh|\.py)$")


# meiva:note
# The generated watcher files among `names` whose watcher SESSION is not in `alive`.
#
# A watcher reads its script + bootstrap exactly once, at launch - but launch is asynchronous:
# `spawn.run` sends the command into a fresh session whose shell takes a moment to come up, so
# the files must stay on disk until the child has actually started. Deleting every old pair at
# every launch (the previous behavior) could delete a sibling's not-yet-read files when watchers
# are created back-to-back, making that sibling's python start fail with "can't open file ...
# _boot.py" (observed 2026-08-08/09: watcher_0_boot.py / watcher_3_boot.py - Bug A, task #1116).
# So only what is provably dead goes: a pair whose session id is not alive. A running watcher's
# pair stays on disk (a few KB) and is pruned once its session closes; a hard-killed watcher's
# pair is pruned once its session is gone too. Names that do not match the generated patterns
# are left alone.
#
# The decision is only as safe as the order its inputs were read in: `names` must be listed
# BEFORE `alive` (see `spawn.run`), because a generated file is written only after its session
# exists, so every file in `names` belongs to a session that the later listing contains while
# it lives.
def stale_names(names: Iterable[str], alive: Collection[int]) -> tuple[str, ...]:
    stale: list[str] = []
    for name in names:
        match = _GENERATED_NAME.match(name)
        if match is not None and int(match.group(1)) not in alive:
            stale.append(name)
    return tuple(stale)


# meiva:note
# The line the launch file carries: the command teed to the output log, followed by the
# completion notice, sent from the shell level so a crashed or hard-killed child cannot skip it.
# The session closes itself after the notice (`keep=False`); a notice that fails to send leaves
# the session open as the post-mortem site.
def launch_line(
    files: LaunchFiles,
    *,
    agent_id: int,
    name: str,
    output_path: Path,
    cli: Path,
    notify: NotifyPolicy | None,
) -> str:
    notice = Notice(
        agent_id=agent_id,
        label=f"Watcher '{name}'",
        source=f"watcher:{files.session_id}",
        cli=cli,
    )
    return notified_line(files.command, notice, output_path=output_path, keep=False, notify=notify)


# meiva:note
# Source of the generated bootstrap file: establish the agent identity, arm the optional timeout
# watchdog, then run the watcher script as ``__main__``.
#
# Agent identity is INLINED as ``MEIVA_AGENT_ID``, because the session machinery deliberately does
# NOT forward it: the env allowlist ``base/host/env/registry.py`` ``child_env`` (Task #856) drops
# every agent-scope / non-modeled ``MEIVA_*`` knob from session children. Without the inline, a
# watcher child would see ``meiva.self.AGENT_ID=None`` and its wake-up ``send_message`` would hit
# ``/api/agents/None/messages`` -> 422 -> the watcher dies before ever waking its agent (Task #964
# - fleet-wide silent loss of every scheduled wake-up). The bootstrap is per-agent generated code
# and the id is the agent's own, so nothing sensitive leaks. Overwrite, not setdefault: the
# watcher belongs to the spawning agent, so a stale env value (e.g. a parent-shell id accidentally
# forwarded) must not win, and the inline must land BEFORE ``import meiva``: importing meiva with
# a stale MEIVA_AGENT_ID in the environment (a session backend freezes the env of its first
# session, so a pane can carry another agent's id) establishes the WRONG identity at import time,
# and a later assignment would be too late to move it (2026-08-09: every watcher child on the
# shared server woke agent 2959 instead of its owner). The child's first ``import meiva``
# composes that identity (``owns_loop=False``), cold clients and plugin surface from one boot
# owner. The profile is pinned to ``agent`` for the same reason: the child inherits
# MEIVA_PROCESS_PROFILE from the creating process's env, but a watcher is an agent subprocess and
# needs the agent profile to import meiva without hitting the per-process config guard (the
# runner profile does not construct the agent domain - Task #856 fail-fast).
#
# The explicit ``meiva.ensure_plugins_loaded()`` remains an idempotent readiness check before the
# watcher script runs. It reuses the completed parent import rather than constructing another
# configuration or plugin installation. The ``meiva`` module is still imported and passed via
# ``init_globals`` to ``runpy.run_path`` so that watcher code can use ``meiva`` without an
# explicit import - this is the public contract of ``launch()``.
#
# runpy is wrapped in try/finally - no except anywhere: an exception still propagates, Python
# prints the traceback to stderr (teed to the watcher's log file and session capture) and exits
# non-zero, and the shell-level completion notice carries the tail of the log. A
# ``SystemExit(n)`` likewise becomes exit code n. The finally block deletes the generated script
# + bootstrap files: a watcher reads them exactly once at launch, so removing them on exit keeps
# the watchers dir empty instead of accumulating a script graveyard (the old global
# $MEIVA_HOME/watchers dir grew past 180 files). The watchdog is a daemon timer that prints its
# reason (into the log) and hard-exits with code 124 (the ``timeout(1)`` convention), so a script
# stuck in a Python-level loop still dies on time - the one thing it cannot preempt is native
# code that never releases the GIL; a killed watcher skips the finally and leaves its pair
# behind, which the next launch prunes (`stale_names`).
#
# Every bootstrap also arms the ORPHAN GUARD (task #1726): a daemon thread that compares
# ``os.getppid()`` against the parent the process booted under every few seconds and hard-exits
# with code 125 on a mismatch. The login shell IS the session; when it ends without taking the
# watcher child with it (the pty-sessions service crashing, an external SIGKILL of the shell), the
# child is reparented to init - still alive, still firing cron/at (2026-08-26: 49 of 85 watcher
# processes on the fleet host were multi-generation orphans of exactly this shape). The guard
# makes session end -> child death within a few seconds, on every such path; a kill-path cascade
# cannot cover a crash or an external SIGKILL. It is armed before ``import meiva`` so a hung
# import cannot keep an orphan alive, and it is the only thing standing between a dead session
# and a watcher that keeps firing forever: nothing tracks or restarts watchers
# (docs/decisions/runtime/updates/recovery/2026-09-27-watchers-are-never-restarted.md).
def build_boot(script_path: Path, watchdog_secs: float | None, agent_id: int) -> str:
    watchdog = ""
    if watchdog_secs is not None:
        timeout_msg = (
            f"[watcher timed out] this watcher reached its {watchdog_secs:g}s "
            "limit and stopped. Re-launch it if you still need it."
        )
        watchdog = (
            "\n"
            "def _timeout():\n"
            f"    print({timeout_msg!r}, file=sys.stderr, flush=True)\n"
            "    os._exit(124)\n"
            "\n"
            f"_watchdog = threading.Timer({watchdog_secs!r}, _timeout)\n"
            "_watchdog.daemon = True\n"
            "_watchdog.start()\n"
        )
    return (
        "# Auto-generated watcher bootstrap. Do not edit manually.\n"
        "import os\n"
        "import runpy\n"
        "import sys\n"
        "import threading\n"
        "import time\n"
        "\n"
        f'os.environ["MEIVA_AGENT_ID"] = "{agent_id}"\n'
        f'os.environ["MEIVA_PROCESS_PROFILE"] = "agent"\n'
        "\n"
        "_parent_pid = os.getppid()\n"
        "\n"
        "def _orphan_guard() -> None:\n"
        "    while True:\n"
        "        time.sleep(5)\n"
        "        if os.getppid() != _parent_pid:\n"
        "            try:\n"
        "                print(\n"
        "                    '[watcher] session gone (pty session ended) — exiting (orphan guard)',\n"
        "                    file=sys.stderr,\n"
        "                    flush=True,\n"
        "                )\n"
        "            finally:\n"
        "                os._exit(125)\n"
        "\n"
        "_orphan_thread = threading.Thread(target=_orphan_guard, daemon=True)\n"
        "_orphan_thread.start()\n"
        "\n"
        "import meiva\n"
        "\n"
        f"{watchdog}"
        "meiva.ensure_plugins_loaded()\n"
        "try:\n"
        f"    runpy.run_path({str(script_path)!r}, run_name='__main__', init_globals={{'meiva': meiva}})\n"
        "finally:\n"
        "    # Self-cleanup: delete both generated files (read once at launch).\n"
        "    # Only OSError is swallowed — a watcher exception still propagates.\n"
        f"    for _p in (__file__, {str(script_path)!r}):\n"
        "        try:\n"
        "            os.unlink(_p)\n"
        "        except OSError:\n"
        "            pass\n"
    )
