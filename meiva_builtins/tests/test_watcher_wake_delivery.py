# meiva:note
# Wake-delivery tests for the skill reference watchers, plus the meiva-dynamic-workflow orchestrator scripts' handoff-dir resolution.
#
# Five reference watchers wake the launching agent with a single send at their trigger point: ``watch_idle.py`` (meiva-goal, which the long-running-agent and meiva-fleet skills read), ``watch_work.py`` (meiva-guide.external-agents), and ``gather_files.py`` (meiva-dynamic-workflow). The send is made once; the SDK's own bounded same-key retries are the only retry. A send that still fails is not silent: the one-shot watchers log it and exit 2 (the exit notice reaches the launcher), and the ``watch_work.py`` supervision loop reports False and tries again at its next trigger. These tests pin the single send, the channel routing, and the exit-2 contract against a real gateway listener on port 0: the SDK client dials it through its real transport, the stub answers (or refuses) the sends, and every assertion reads what reached it.
#
# The handoff-dir tests below (2026-09-28 fix, PR #3550 follow-up P2-1) lock `codebase_sweep_lite.py`, `deep_research_lite.py` (`meiva-dynamic-workflow/scripts/`), `codebase_sweep_orchestrator.py`, `deep_research_orchestrator.py`, and `orchestrator_template.py` (`meiva-dynamic-workflow/references/`, alongside `gather_files.py` above) to `base.paths.workspace_dir` instead of a hardcoded `Path.home() / ".meiva/workspaces"` that never read `MEIVA_HOME` at all. On a non-default home cluster (a worktree cluster, home `~/.meiva-<dir>`) that silently wrote scratch/handoff state into the WRONG cluster's workspace tree.

from __future__ import annotations

import ast
import importlib.util
import os
import sys
import threading
import time
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

import meiva
from base.clock.tests.fakes import system_clock
from base.clock.tests.seeded_rng import seeded_rng
from base.paths import workspace_dir
from meiva.tests.fixtures.gateway_wire import GatewayStub, GatewayWire
from meiva.tests.fixtures.gateway_wire import gateway_stub as gateway_stub
from meiva.tests.fixtures.pin_agent import pin_agent

_REPO = Path(__file__).parents[2]
_DYNAMIC_WORKFLOW_SCRIPTS = (
    _REPO / "meiva_builtins" / "skills" / "coordination" / "meiva-dynamic-workflow" / "scripts"
)
_DYNAMIC_WORKFLOW_REFERENCES = (
    _REPO / "meiva_builtins" / "skills" / "coordination" / "meiva-dynamic-workflow" / "references"
)
_LAUNCHER = 42
_LAUNCHER_MESSAGES = f"/api/agents/{_LAUNCHER}/messages"
_SDK_ATTEMPTS = 2


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


watch_idle = _load(
    "watch_idle_under_test",
    _REPO / "meiva_builtins/skills/coordination/meiva-goal/scripts/watch_idle.py",
)
watch_work = _load(
    "watch_work_retry_under_test",
    _REPO / "meiva_builtins/skills/platform/meiva-guide/external-agents/scripts/watch_work.py",
)
gather_files = _load(
    "gather_files_under_test",
    _DYNAMIC_WORKFLOW_REFERENCES / "gather_files.py",
)


@pytest.fixture
def launcher(sdk_environment: object, gateway_stub: GatewayStub) -> Iterator[GatewayWire]:
    """The gateway as the launching agent sees it: a listener on port 0 and the SDK client that
    dials it, with the process pinned to agent 42 (the launcher the watchers wake). The SDK
    makes its bounded same-key attempts without waiting between them."""
    with GatewayWire.open(gateway_stub, retries=_SDK_ATTEMPTS) as wire:
        pin_agent(_LAUNCHER, clients=wire.context.clients)
        yield wire


def _accept_messages(wire: GatewayWire, agent_id: int = _LAUNCHER) -> None:
    wire.stub.reply("POST", f"/api/agents/{agent_id}/messages", {"status": "enqueued"}, status=201)


def _refuse_messages(wire: GatewayWire, agent_id: int = _LAUNCHER) -> None:
    wire.stub.reply("POST", f"/api/agents/{agent_id}/messages", {}, status=503)


# meiva:note
# The watcher added no retry of its own: every request the gateway saw carries the one key of the SDK's single bounded send (its configured attempts, no more).
def _assert_one_logical_send_that_was_refused(wire: GatewayWire) -> None:
    assert len(wire.sent) == _SDK_ATTEMPTS
    assert len({sent.headers["idempotency-key"] for sent in wire.sent}) == 1


# meiva:note
# Rewrite the work file after `delay` seconds with a modification time that cannot equal the old one.
def _rewrite_after(work: Path, delay: float, text: str) -> threading.Timer:

    def rewrite() -> None:
        work.write_text(text)
        mtime = work.stat().st_mtime + 1
        os.utime(work, (mtime, mtime))

    timer = threading.Timer(delay, rewrite)
    timer.start()
    return timer


# meiva:note
# A supervision pace that polls every few milliseconds and never stalls unless told to.
def _quick(**changes: float) -> Any:
    values = {
        "poll_seconds": 0.01,
        "stall_seconds": 3600.0,
        "heartbeat_seconds": 3600.0,
        "hard_limit_seconds": 3600.0,
    }
    values.update(changes)
    return watch_work.Cadence(**values)


def test_watch_idle_sends_the_reminder_once(launcher: GatewayWire) -> None:
    """The idle reminder reaches the launching agent with one send."""
    _accept_messages(launcher)

    watch_idle._notify(7)

    [sent] = launcher.sent
    assert sent.path == _LAUNCHER_MESSAGES  # meiva.self.AGENT_ID — the launching agent
    assert "target agent 7 idled" in sent.body["content"]


# meiva:note
# A refused send is not a silent nothing and not a ten-minute retry loop: the watcher logs it and exits 2 after the single send.
def test_watch_idle_failed_send_exits_2_without_a_retry_window(
    launcher: GatewayWire, capsys: pytest.CaptureFixture[str]
) -> None:
    _refuse_messages(launcher)

    with pytest.raises(SystemExit) as excinfo:
        watch_idle._notify(7)

    assert excinfo.value.code == 2
    _assert_one_logical_send_that_was_refused(launcher)
    out = capsys.readouterr().out
    assert "wake delivery failed" in out
    assert "503 Service Unavailable" in out


# meiva:note
# A checkpoint over a tmp handoff dir with both files landed.
def _checkpoint(tmp_path: Path) -> Any:
    (tmp_path / "a.json").write_text("{}")
    (tmp_path / "b.json").write_text("{}")
    return gather_files.Checkpoint(str(tmp_path), ("a.json", "b.json"), "", 0, _LAUNCHER)


def test_gather_files_sends_the_checkpoint_once(launcher: GatewayWire, tmp_path: Path) -> None:
    _accept_messages(launcher)

    gather_files.watch(_checkpoint(tmp_path), 0)

    [sent] = launcher.sent
    assert sent.path == _LAUNCHER_MESSAGES
    assert "checkpoint reached" in sent.body["content"] and "(2/2)" in sent.body["content"]


def test_gather_files_failed_send_exits_2_without_a_retry_window(
    launcher: GatewayWire, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    _refuse_messages(launcher)

    with pytest.raises(SystemExit) as excinfo:
        gather_files.watch(_checkpoint(tmp_path), 0)

    assert excinfo.value.code == 2
    _assert_one_logical_send_that_was_refused(launcher)
    out = capsys.readouterr().out
    assert "wake delivery failed" in out
    assert "503 Service Unavailable" in out


def test_watch_work_notify_sends_the_generic_wake_once(launcher: GatewayWire) -> None:
    """The generic channel sends once; True means the message landed."""
    _accept_messages(launcher, 41)

    assert watch_work._notify(41, "hello", canonical=False) is True

    [sent] = launcher.sent
    assert sent.path == "/api/agents/41/messages"
    assert sent.body["content"] == "hello"


def test_watch_work_notify_routes_canonical_through_system_note(launcher: GatewayWire) -> None:
    """Canonical supervision speaks through the system-note channel."""
    launcher.stub.reply("POST", "/api/agents/41/system-note", {"inbound_id": 9}, status=201)

    assert watch_work._notify(41, "cleanup done", canonical=True) is True

    [sent] = launcher.sent
    assert sent.path == "/api/agents/41/system-note"
    assert sent.body["content"] == "cleanup done"
    assert sent.body["note_tag"] == "task"
    assert sent.body["resurrect"] is False
    assert sent.headers["idempotency-key"]


# meiva:note
# False (not an exception) after the single send: the in-loop call sites keep supervising and send again at their next trigger, while the one-shot sites opt in to exiting.
def test_watch_work_failed_send_reports_false_without_a_retry_window(
    launcher: GatewayWire, capsys: pytest.CaptureFixture[str]
) -> None:
    _refuse_messages(launcher, 41)

    assert watch_work._notify(41, "hello", canonical=False) is False

    _assert_one_logical_send_that_was_refused(launcher)
    assert "wake delivery failed" in capsys.readouterr().out


def test_watch_work_terminal_wake_delivers_once(launcher: GatewayWire, tmp_path: Path) -> None:
    """A new actionable status wakes with a single send."""
    _accept_messages(launcher)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")

    timer = _rewrite_after(work, 0.1, "STATUS: DONE\n")
    try:
        watch_work.watch(str(work), cadence=_quick())
    finally:
        timer.cancel()
        timer.join()

    [sent] = launcher.sent
    assert sent.path == _LAUNCHER_MESSAGES
    assert "STATUS: DONE" in sent.body["content"]


def test_watch_work_baselines_actionable_status_until_its_mtime_changes(
    launcher: GatewayWire, tmp_path: Path
) -> None:
    """A re-armed stale DONE waits for a subsequent file rewrite to wake."""
    _accept_messages(launcher)
    work = tmp_path / "work.md"
    work.write_text("STATUS: DONE\n")
    delay = 0.15

    started = time.monotonic()
    timer = _rewrite_after(work, delay, "STATUS: DONE\n")
    try:
        watch_work.watch(str(work), cadence=_quick())
    finally:
        timer.cancel()
        timer.join()

    assert time.monotonic() - started >= delay
    [sent] = launcher.sent
    assert "STATUS: DONE" in sent.body["content"]


def test_watch_work_heartbeats_an_unchanged_baselined_actionable_status(
    launcher: GatewayWire, tmp_path: Path
) -> None:
    """A stale actionable status wakes once through the heartbeat branch."""
    _accept_messages(launcher)
    work = tmp_path / "work.md"
    work.write_text("STATUS: DONE\n")

    watch_work.watch(str(work), cadence=_quick(heartbeat_seconds=0.05))

    [sent] = launcher.sent
    assert "unchanged since arming" in sent.body["content"]


def test_watch_work_canonical_need_input_wakes_on_its_first_eligible_poll(
    launcher: GatewayWire, tmp_path: Path
) -> None:
    """Canonical supervision must not baseline an already-current NEED_INPUT."""
    key = watch_work.coding_session_owner.canonical_key(
        tmp_path / "workspace", tool="codex", cluster=tmp_path / "cluster"
    )
    work = tmp_path / "work.md"
    owner = watch_work.coding_session_owner.launch_generation(
        key,
        owner_agent_id=41,
        tasks_file=tmp_path / "tasks.md",
        work_file=work,
        ttl_seconds=3600,
        writer=watch_work.AtomicWriter(watch_work.SYSTEM_FS),
        now=system_clock().now(),
        rng=seeded_rng(),
    )
    generation = owner.generation
    assert generation is not None
    work.write_text("STATUS: NEED_INPUT\n")
    current = work.stat().st_mtime + 1
    os.utime(work, (current, current))
    launcher.stub.reply("POST", "/api/agents/41/system-note", {"inbound_id": 9}, status=201)
    launcher.stub.reply("GET", "/api/agents/41", {"status": "idling"})

    def end_supervision() -> None:
        watch_work.coding_session_owner.terminate_generation(
            key,
            generation,
            reason="test-over",
            writer=watch_work.AtomicWriter(watch_work.SYSTEM_FS),
            now=system_clock().now(),
        )

    timer = threading.Timer(0.3, end_supervision)
    timer.start()
    try:
        watch_work.watch(
            str(work),
            cluster=str(tmp_path / "cluster"),
            workspace=str(tmp_path / "workspace"),
            generation=generation,
            owner_agent_id=41,
            cadence=_quick(),
        )
    finally:
        timer.cancel()
        timer.join()

    notes = [sent for sent in launcher.sent if sent.path == "/api/agents/41/system-note"]
    [note] = notes
    assert note.body["content"] == (
        f"coding agent reported STATUS: NEED_INPUT in {work} -- read the file and reply"
    )


def test_watch_work_terminal_wake_exits_2_on_exhaustion(
    launcher: GatewayWire, tmp_path: Path
) -> None:
    """The generic one-shot path: a wake that fails exits 2 instead of ending
    as if it had been delivered."""
    _refuse_messages(launcher)
    work = tmp_path / "work.md"
    work.write_text("STATUS: WORKING\n")

    timer = _rewrite_after(work, 0.1, "STATUS: DONE\n")
    try:
        with pytest.raises(SystemExit) as excinfo:
            watch_work.watch(str(work), cadence=_quick())
    finally:
        timer.cancel()
        timer.join()

    assert excinfo.value.code == 2
    _assert_one_logical_send_that_was_refused(launcher)


# --------------------------------------------------------------------------- #
# meiva-dynamic-workflow orchestrator scripts' handoff dir (P2-1)
# --------------------------------------------------------------------------- #


def _exec_prefix_through(path: Path, target: str, namespace: dict[str, Any]) -> None:
    """Exec `path`'s top-level statements up to and including its first
    `target = ...` assignment, then stop.

    The three full orchestrators (`codebase_sweep_orchestrator.py`,
    `deep_research_orchestrator.py`, `orchestrator_template.py`) run their
    fork/spawn logic as more top-level statements immediately after the
    handoff-dir assignment — they are meant to be pasted whole into
    `execute_code`. Execing the whole file would spawn real agents; stopping
    right after `target` is assigned is enough to prove how the handoff dir
    resolves without touching any of that.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    prefix: list[ast.stmt] = []
    for node in tree.body:
        prefix.append(node)
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == target
        ):
            break
    else:
        raise AssertionError(f"no top-level assignment to {target!r} found in {path}")
    module = ast.Module(body=prefix, type_ignores=[])
    ast.fix_missing_locations(module)
    exec(compile(module, str(path), "exec"), namespace)


@pytest.fixture
def agent_home(sdk_environment: object, unit_home: Path) -> int:
    """The SDK root of a test home, with this process pinned as agent 1: the reference scripts
    do a top-level `import meiva` and read only `meiva.self.AGENT_ID`. The context is repinned over
    the test's home, so the Meiva home it carries is `unit_home`."""
    pin_agent(1)
    return meiva.self.AGENT_ID


@pytest.mark.parametrize(
    ("filename", "target", "suffix"),
    [
        ("codebase_sweep_lite.py", "HD", "codebase_sweep_lite"),
        ("deep_research_lite.py", "HD", "deep_research_lite"),
    ],
)
def test_lite_script_handoff_dir_uses_workspace_dir(
    agent_home: int, unit_home: Path, filename: str, target: str, suffix: str
) -> None:
    """Importing a lite script only defines functions at module top level
    (unlike the full orchestrators below), so a full exec is safe here."""
    namespace: dict[str, Any] = {"__name__": f"{filename}_under_test"}
    exec(
        compile(
            (_DYNAMIC_WORKFLOW_SCRIPTS / filename).read_text(encoding="utf-8"), filename, "exec"
        ),
        namespace,
    )
    assert namespace[target] == workspace_dir(agent_home, home=unit_home) / suffix


@pytest.mark.parametrize(
    ("filename", "target", "task_var"),
    [
        ("codebase_sweep_orchestrator.py", "HANDOFF", "TASK"),
        ("deep_research_orchestrator.py", "HANDOFF", "TASK"),
    ],
)
def test_full_orchestrator_handoff_dir_uses_workspace_dir(
    agent_home: int, unit_home: Path, filename: str, target: str, task_var: str
) -> None:
    namespace: dict[str, Any] = {"__name__": f"{filename}_under_test"}
    _exec_prefix_through(_DYNAMIC_WORKFLOW_REFERENCES / filename, target, namespace)
    assert namespace[target] == workspace_dir(agent_home, home=unit_home) / namespace[task_var]


def test_orchestrator_template_handoff_dir_uses_workspace_dir(
    agent_home: int, unit_home: Path
) -> None:
    namespace: dict[str, Any] = {"__name__": "orchestrator_template_under_test"}
    _exec_prefix_through(
        _DYNAMIC_WORKFLOW_REFERENCES / "orchestrator_template.py", "handoff", namespace
    )
    assert namespace["handoff"] == workspace_dir(agent_home, home=unit_home) / "task_handoff"
