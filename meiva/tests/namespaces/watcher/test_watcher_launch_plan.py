# meiva:note
# The pure side of a watcher launch: what the generated bootstrap, the script and the launch line
# contain for a request, asserted on the values `launch_plan` returns. No session is started.

from __future__ import annotations

import ast
import datetime
import pathlib

from meiva.watcher import launch_plan, plans

_NOW = datetime.datetime(2026, 10, 24, 5, 6, 7, tzinfo=datetime.UTC)


def _files(
    code: str = "import meiva\n",
    *,
    timeout: float | str = "1h",
    session_id: int = 7,
    directory: pathlib.Path = pathlib.Path("/watchers"),
) -> launch_plan.LaunchFiles:
    request = plans.plan_launch(code, timeout, "test-watcher", None, _NOW)
    return launch_plan.plan_files(directory, session_id, request, 42, "/venv/bin/python")


def test_launch_arms_watchdog_in_boot_file() -> None:
    # meiva:note
    # The generated bootstrap must arm a daemon timer that hard-exits with
    # code 124 at the deadline — that is what bounds a custom watcher's
    # lifetime. The timeout reason is printed so it lands in the log file.
    boot = _files(timeout=120).boot_text
    assert "threading.Timer(120.0" in boot
    assert "os._exit(124)" in boot
    assert "120s limit" in boot


def test_watchdog_message_formats_fractional_seconds() -> None:
    assert "0.5s limit" in _files(timeout=0.5).boot_text


def test_boot_without_watchdog_has_no_timer(tmp_path: pathlib.Path) -> None:
    # meiva:note
    # at/cron scripts self-terminate, so their bootstrap arms no watchdog.
    # The orphan guard (task #1726) is armed regardless — it is not a
    # watchdog: it only hard-exits a watcher whose session chain died, and
    # never bounds a healthy watcher's lifetime.
    boot = launch_plan.build_boot(tmp_path / "x.py", None, 42)
    assert "Timer" not in boot
    assert "os._exit(124)" not in boot
    assert "os._exit(125)" in boot


# meiva:note
# Task #1726: every generated bootstrap must arm the orphan guard — a
# daemon thread that compares getppid() against the boot-time parent and
# hard-exits (code 125) on a mismatch, so a watcher child dies within
# seconds of its pty host dying instead of firing cron/at forever as a
# ppid=1 orphan. Armed BEFORE `import meiva`: a hung import must not keep an
# orphan alive.
def test_boot_arms_orphan_guard(tmp_path: pathlib.Path) -> None:
    boot = launch_plan.build_boot(tmp_path / "x.py", None, 42)
    assert "_parent_pid = os.getppid()" in boot
    assert "def _orphan_guard" in boot
    assert "os.getppid() != _parent_pid" in boot
    assert "os._exit(125)" in boot
    guard_line = boot.index("_orphan_guard")
    assert guard_line < boot.index("import meiva")
    ast.parse(boot)


def test_boot_orphan_guard_message_names_session_gone(tmp_path: pathlib.Path) -> None:
    # meiva:note
    # The guard's stderr line lands in the watcher's log — a debugger must be
    # able to tell an orphan-guard exit (125) from a watchdog timeout (124).
    boot = launch_plan.build_boot(tmp_path / "x.py", None, 42)
    assert "[watcher] session gone (pty session ended)" in boot
    assert "os._exit(125)" in boot


def test_boot_loads_plugins_before_running_script(tmp_path: pathlib.Path) -> None:
    # meiva:note
    # A fresh child's `import meiva` is the factory module without the agent
    # process's plugin setattrs, so the bootstrap must load plugin namespaces
    # (meiva.tasks etc.) before it runpys the watcher script — otherwise the script
    # AttributeErrors on any plugin namespace. Order matters: load then run.
    boot = launch_plan.build_boot(tmp_path / "x.py", None, 42)
    assert "meiva.ensure_plugins_loaded()" in boot
    assert boot.index("meiva.ensure_plugins_loaded()") < boot.index("runpy.run_path")
    ast.parse(boot)


def test_boot_propagates_failures_without_catching(tmp_path: pathlib.Path) -> None:
    # meiva:note
    # try/finally around runpy only cleans up the generated files — no except
    # anywhere, so a crash still prints its traceback into the log and exits
    # non-zero; the shell-level notice reports the code. Only the cleanup's
    # own OSError is swallowed (unlink failure is harmless).
    boot = launch_plan.build_boot(tmp_path / "x.py", 60.0, 42)
    assert "try:" in boot
    assert "finally:" in boot
    # meiva:note
    # The runpy try has NO except — a crash propagates (R1-8: only the
    # finally's own cleanup — deleting the generated files — is fail-soft).
    assert "except Exception" not in boot.split("finally:")[0]
    assert "except BaseException" not in boot
    assert "runpy.run_path" in boot
    ast.parse(boot)


def test_boot_self_cleans_generated_files(tmp_path: pathlib.Path) -> None:
    # meiva:note
    # The finally block deletes the script + the bootstrap itself — a watcher
    # reads them exactly once at launch, so normal exits leave the watchers
    # dir empty instead of accumulating a script graveyard.
    script = tmp_path / "watcher_3.py"
    boot = launch_plan.build_boot(script, None, 42)
    assert "finally:" in boot
    assert "os.unlink(_p)" in boot
    assert "except OSError" in boot
    assert str(script) in boot
    assert "__file__" in boot
    ast.parse(boot)


def test_launch_carries_agent_and_session_to_child() -> None:
    # meiva:note
    # Identity crosses the process boundary inlined into the generated
    # bootstrap (the session env allowlist does NOT forward MEIVA_AGENT_ID —
    # Task #856/#964): the child must see its agent id as MEIVA_AGENT_ID.
    # The watcher's session id rides as an env var on the run command so the
    # time watchers can tag their wake-ups.
    files = _files(session_id=7)
    assert files.command.startswith("MEIVA_WATCHER_SESSION_ID=7 ")
    boot = files.boot_text
    # meiva:note
    # Identity is inlined; meiva is imported for init_globals
    assert 'os.environ["MEIVA_AGENT_ID"] = "42"' in boot
    assert "process_context" not in boot
    assert "import meiva" in boot
    assert "runpy.run_path" in boot
    assert "init_globals" in boot


def test_launch_writes_script_verbatim_and_runs_via_runpy(tmp_path: pathlib.Path) -> None:
    # meiva:note
    # The script file is the agent's code unchanged — nothing prepended — so a
    # leading `from __future__` import (must be the first statement of a file)
    # stays valid. Watchdog and runpy live in the separate generated bootstrap
    # file; identity is inlined there (the session env allowlist does not
    # forward MEIVA_AGENT_ID — Task #856/#964).
    code = "from __future__ import annotations\nimport meiva\n"
    files = _files(code, session_id=9, directory=tmp_path)

    assert files.script_path == tmp_path / "watcher_9.py"
    assert files.script_text == code
    assert "runpy.run_path" in files.boot_text
    assert 'os.environ["MEIVA_AGENT_ID"] = "42"' in files.boot_text
    assert "init_globals" in files.boot_text
    assert "watcher_9.py" in files.boot_text
    assert files.launch_path == tmp_path / "watcher_9_launch.sh"
    assert "watcher_9_boot.py" in files.command


def test_spawn_line_tees_and_notifies(tmp_path: pathlib.Path) -> None:
    # meiva:note
    # The command line must tee the child's output to the per-agent log file
    # and session capture, then end with the CLI completion notice + session
    # close: the notice is sent from the shell level so a crashed or
    # hard-killed child cannot skip it, and `; exit $_ec` closes the session
    # even when delivery fails — a lingering shell is a live session nobody
    # ever intended to keep (Task #1115 bug B).
    files = _files(session_id=5)
    cmd = launch_plan.launch_line(
        files,
        agent_id=42,
        name="test-notice",
        output_path=tmp_path / "5_test-notice.log",
        cli=tmp_path / "meiva",
        notify=None,
    )

    assert "exec 3> >(tee" in cmd
    assert ") >&3 2>&1" in cmd
    assert "_ec=$?" in cmd
    assert "agents send 42" in cmd
    assert "--source watcher:5" in cmd
    assert "finished. Full output at" in cmd
    assert "exited with code" not in cmd
    assert "--tail-file" in cmd
    assert cmd.endswith("; exit $_ec")


# meiva:note
# The bootstrap must set MEIVA_AGENT_ID itself: the session env allowlist
# (base/host/env/registry.py child_env, Task #856) deliberately does
# not forward agent-scope knobs to session children, so a child that relied
# on inheritance would see meiva.self.AGENT_ID=None and its wake-up
# send_message would 422 on /api/agents/None/messages (Task #964). The
# inline assignment must run before any SDK use — including `import meiva`
# itself: a stale MEIVA_AGENT_ID in the session env (a server freezes
# its first session's env) would otherwise establish the WRONG identity at
# import time (2026-08-09: every watcher child on the shared server woke
# agent 2959 instead of its owner).
def test_boot_inlines_agent_identity(tmp_path: pathlib.Path) -> None:
    boot = launch_plan.build_boot(tmp_path / "x.py", None, 42)
    assert 'os.environ["MEIVA_AGENT_ID"] = "42"' in boot
    env_line = boot.index('os.environ["MEIVA_AGENT_ID"]')
    assert env_line < boot.index("import meiva")
    assert env_line < boot.index("meiva.ensure_plugins_loaded()")
    assert env_line < boot.index("runpy.run_path")
    ast.parse(boot)
