from __future__ import annotations

import datetime as dt
import importlib.util
import json
import shlex
import sys
import time
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any
from uuid import UUID

import httpx
import pytest

from base.api_contracts.agent_contract import AgentStatus, ErrorReason
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.lm.harness.codex.auth import SECRET_ENV_NAMES
from base.sessions import coding_session_owner
from base.sessions.coding_session_owner_record import CodingSessionStatus
from base.sessions.pty.tests.pty_service import PtyServiceProcess
from meiva.sdk_surface.tests.agent_binding import BindAgent
from meiva.shell.coding_tools import _common, codex
from meiva.shell.coding_tools.plans.launch_request import LaunchRequest, worker_bootstrap
from meiva.shell.coding_tools.plans.pacing import CodexWaits, Pacing
from meiva.shell.coding_tools.tests.fake_tools import (
    CODEX_SESSION,
    gateway_double,
    install_codex,
    site_for,
)
from meiva.shell.session_env import SessionHost
from meiva.shell.sessions import ShellSessions
from meiva.shell.tests.live_shell import seed_agent

_SKILL_DIR = (
    Path(__file__).parents[4]
    / "meiva_builtins"
    / "skills"
    / "platform"
    / "meiva-guide"
    / "external-agents"
)
_WAITS = CodexWaits(
    server=Pacing(timeout=15.0, poll=0.05, settle=0.0),
    ready=Pacing(timeout=20.0, poll=0.05, settle=0.1),
    status=Pacing(timeout=15.0, poll=0.05, settle=0.0),
    submit=Pacing(timeout=15.0, poll=0.05, settle=0.1),
)


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


spawn_codex = _load("spawn_codex_under_test", _SKILL_DIR / "scripts" / "spawn_codex.py")
watch_work = _load("watch_work_under_test", _SKILL_DIR / "scripts" / "watch_work.py")
_OWNER = 41


def _owner(tmp_path: Path) -> coding_session_owner.CodingSessionOwner:
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    key = coding_session_owner.canonical_key(
        workspace,
        tool="codex",
        cluster=tmp_path / "cluster",
    )
    generation = "11111111-2222-3333-4444-555555555555"
    now = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
    return coding_session_owner.CodingSessionOwner(
        key=key,
        status=CodingSessionStatus.ACTIVE,
        generation=generation,
        owner_agent_id=41,
        display_label="workspace",
        expected_suffix="codex-workspace-11111111",
        session_id=7,
        session_name="meiva-agent-41-shell-7-codex-workspace-11111111",
        state_dir=coding_session_owner.generation_state_dir(key, generation),
        tasks_file=workspace / "tasks.md",
        work_file=workspace / "work.md",
        created_at=now,
        expires_at=now + dt.timedelta(hours=4),
    )


def test_launch_runs_on_the_default_home_with_per_session_overrides(tmp_path: Path) -> None:
    workspace = Path(_owner(tmp_path).key.workspace)

    argv = shlex.split(codex.codex_command(workspace).split(" && ", 1)[1])

    assert "CODEX_HOME" not in " ".join(argv)
    assert argv[:3] == ["exec", "env", "-u"]
    assert argv[3:9] == [
        "OPENAI_API_KEY",
        "-u",
        "CODEX_API_KEY",
        "-u",
        "CODEX_ACCESS_TOKEN",
        "codex",
    ]
    overrides = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-c"]
    assert overrides == [
        f'projects={{"{workspace.as_posix()}"={{trust_level="trusted"}}}}',
        "check_for_update_on_startup=false",
    ]


def _dropped() -> str:
    return "env " + " ".join(f"-u {name}" for name in SECRET_ENV_NAMES) + " "


def test_both_codex_processes_start_without_the_inherited_key(tmp_path: Path) -> None:
    owner = _owner(tmp_path)
    workspace = Path(owner.key.workspace)

    server = codex.app_server_command(owner, workspace, "unix:///tmp/probe.sock")
    tui = codex.codex_command(workspace)

    for command in (server, tui):
        assert all(f"-u {name}" in command for name in SECRET_ENV_NAMES), command


def test_launch_command_starts_a_new_session_unless_resuming(tmp_path: Path) -> None:
    workspace = Path(_owner(tmp_path).key.workspace)
    session = "01a0e1ac-adc7-7d33-bd13-8ce2c6a686c5"

    fresh = codex.codex_command(workspace)
    resumed = codex.codex_command(workspace, resume=session)

    assert fresh.startswith(f"cd {workspace} && ")
    assert f"exec {_dropped()}codex --dangerously-bypass" in fresh
    assert " resume " not in fresh
    assert f"exec {_dropped()}codex resume {session} --dangerously-bypass" in resumed
    assert "MEIVA_CALLER_IDENTITY" not in fresh


def test_launch_command_can_explicitly_declare_external_caller(tmp_path: Path) -> None:
    record = _owner(tmp_path)
    command = codex.codex_command(Path(record.key.workspace), "run-42")
    assert "MEIVA_CALLER_IDENTITY=" in command
    assert '"kind":"external_agent"' in command
    assert '"subject":"codex"' in command
    assert '"instance":"run-42"' in command


def test_fresh_launch_names_the_full_handle_and_hands_the_worker_both_files(
    tmp_path: Path,
) -> None:
    record = _owner(tmp_path)
    workspace = Path(record.key.workspace)
    tasks_file = workspace / "tasks.md"
    work_file = workspace / "work.md"

    assert (
        coding_session_owner.full_session_name(41, 7, "codex-workspace-11111111")
        == "meiva-agent-41-shell-7-codex-workspace-11111111"
    )
    contract = _SKILL_DIR / "references" / "collaboration_protocol.md"
    message = worker_bootstrap(contract, workspace, tasks_file, work_file)
    assert str(contract) in message
    assert str(workspace) in message
    assert str(tasks_file) in message
    assert str(work_file) in message


def test_supervisor_bootstrap_restores_owner_identity(tmp_path: Path) -> None:
    command = codex.supervisor_command(_owner(tmp_path), _SKILL_DIR / "scripts" / "watch_work.py")

    argv = shlex.split(command)
    assert argv[:3] == ["exec", "env", "MEIVA_AGENT_ID=41"]
    assert argv[-2] == "-c" and "['watch']" in argv[-1]


def test_codex_supervisor_uses_projected_session_environment(
    pty_service: PtyServiceProcess, unit_home: Path, short_dir: Path, database: Any
) -> None:
    from base.sessions.backend import PtySessionBackend
    from base.sessions.pty.client import PtyClientConfig
    from meiva.shell.tests.live_shell import isolated_host

    owner = _owner(short_dir)
    report = short_dir / "env.json"
    pty_service.stop()
    service = PtyServiceProcess(unit_home, {"VIRTUAL_ENV": str(short_dir / "foreign" / ".venv")})
    service.start()
    seed_agent(database, 41, next_index=7)
    shell = ShellSessions(
        backend=PtySessionBackend(PtyClientConfig()), database=database, agent_id=41
    )
    probe = (
        "import json, os; from pathlib import Path; "
        f"report = Path({str(report)!r}); pending = report.with_suffix('.tmp'); "
        "pending.write_text(json.dumps(dict("
        "virtual_env=os.environ.get('VIRTUAL_ENV'), cwd=os.getcwd()))); pending.replace(report)"
    )
    name = coding_session_owner.full_session_name(41, 7, codex.supervisor_name(owner))
    try:
        site = site_for(shell, unit_home, isolated_host(unit_home))
        sid, actual_name = codex.launch_supervisor(
            site, owner, 120, f"exec {shlex.quote(sys.executable)} -c {shlex.quote(probe)}"
        )
        assert (sid, actual_name) == (7, name)
        deadline = time.monotonic() + 15
        while not report.exists() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert report.exists()
        assert json.loads(report.read_text()) == {
            "virtual_env": None,
            "cwd": str(site.session_cwd),
        }
    finally:
        shell.kill_all()
        service.stop()


def test_a_supervisor_is_refused_for_an_owner_without_an_agent_identity(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, short_dir: Path
) -> None:
    anonymous = replace(_owner(short_dir), owner_agent_id=None)

    with pytest.raises(RuntimeError, match="launching owner has no agent identity"):
        codex.launch_supervisor(
            site_for(live_shell, unit_home, live_host), anonymous, 120, "exec sleep 60"
        )

    assert live_shell.list() == {}


def _stub_skill(short_dir: Path, report: Path) -> Path:
    skill = short_dir / "skill"
    (skill / "scripts").mkdir(parents=True)
    (skill / "scripts" / "watch_work.py").write_text(
        "import json\nimport time\n\n\n"
        "def watch(work_file, **owner):\n"
        f"    with open({str(report)!r}, 'w') as out:\n"
        "        json.dump(dict(work_file=work_file, **owner), out)\n"
        "    time.sleep(60)\n"
    )
    return skill


def _request(
    workspace: Path,
    skill_dir: Path,
    *,
    prompt: str | None = None,
    resume: str | None = None,
) -> LaunchRequest:
    return LaunchRequest(
        workspace=workspace,
        tasks_file=None if prompt is not None else workspace / "tasks.md",
        work_file=None if prompt is not None else workspace / "work.md",
        ttl_seconds=3600,
        caller_instance=None,
        prompt=prompt,
        skill_dir=skill_dir,
        resume=resume,
    )


def _generation(
    site_cluster: Path, workspace: Path, *, supervised: bool
) -> coding_session_owner.CodingSessionOwner:
    key = coding_session_owner.canonical_key(workspace, tool="codex", cluster=site_cluster)
    return _common.new_generation(
        key,
        owner_agent_id=41,
        tasks_file=workspace / "tasks.md" if supervised else None,
        work_file=workspace / "work.md" if supervised else None,
        ttl_seconds=3600,
    )


def _wait_for_text(path: Path, text: str, seconds: float = 20.0) -> str:
    deadline = time.monotonic() + seconds
    content = ""
    while time.monotonic() < deadline:
        content = path.read_text() if path.exists() else ""
        if text in content:
            return content
        time.sleep(0.05)
    return content


def test_takeover_launch_inlines_the_brief_without_files_or_a_supervisor(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, short_dir: Path
) -> None:
    received = short_dir / "r.txt"
    install_codex(unit_home, received)
    workspace = short_dir / "ws"
    workspace.mkdir()
    site = site_for(live_shell, short_dir, live_host)
    owner = _generation(site.cluster, workspace, supervised=False)
    prompt = "Goal: replace the agent. The briefing is inline; read no files."

    active, remote, session = codex.start_generation(
        site, owner, _request(workspace, _SKILL_DIR, prompt=prompt), _WAITS
    )

    assert session == CODEX_SESSION
    assert remote.startswith("unix://")
    assert _wait_for_text(received, prompt) == prompt + "\n"
    argv = received.with_suffix(".argv").read_text()
    assert f"--remote {remote} --dangerously-bypass-approvals-and-sandbox" in argv
    assert active.status == CodingSessionStatus.ACTIVE
    assert active.supervisor_session_id is None
    assert sorted(path.name for path in workspace.iterdir()) == []
    assert list(live_shell.list().values()) == [owner.expected_suffix]


def test_supervised_launch_runs_a_supervisor_for_exactly_this_generation(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, short_dir: Path
) -> None:
    received = short_dir / "r.txt"
    install_codex(unit_home, received)
    report = short_dir / "supervisor.json"
    skill = _stub_skill(short_dir, report)
    workspace = short_dir / "ws"
    workspace.mkdir()
    site = site_for(live_shell, short_dir, live_host)
    owner = _generation(site.cluster, workspace, supervised=True)

    active, _remote, _session = codex.start_generation(
        site, owner, _request(workspace, skill), _WAITS
    )

    delivered = _wait_for_text(received, "Now read the task file and start working.")
    assert "Read the collaboration contract at" in delivered
    assert str(workspace / "work.md") in delivered
    assert _wait_for_text(report, "owner_agent_id")
    reported = json.loads(report.read_text())
    assert reported == {
        "work_file": str(owner.work_file),
        "cluster": owner.key.cluster,
        "workspace": owner.key.workspace,
        "generation": owner.generation,
        "owner_agent_id": 41,
    }
    assert active.supervisor_session_id is not None
    assert active.supervisor_session_name is not None
    assert set(live_shell.list()) == {active.supervisor_session_id, active.session_id}


@pytest.mark.parametrize("opened", [CODEX_SESSION, "01a0e1eb-472f-7832-81f6-77c7d2e47d75"])
def test_a_resumed_worker_reopens_its_session_and_is_told_it_was_interrupted(
    opened: str,
    live_shell: ShellSessions,
    live_host: SessionHost,
    unit_home: Path,
    short_dir: Path,
) -> None:
    received = short_dir / "r.txt"
    install_codex(unit_home, received, session=opened)
    skill = _stub_skill(short_dir, short_dir / "supervisor.json")
    workspace = short_dir / "ws"
    workspace.mkdir()
    site = site_for(live_shell, short_dir, live_host)
    owner = _generation(site.cluster, workspace, supervised=True)
    request = _request(workspace, skill, resume=CODEX_SESSION)

    if opened != CODEX_SESSION:
        with pytest.raises(RuntimeError, match=f"not the requested {CODEX_SESSION}"):
            codex.start_generation(site, owner, request, _WAITS)
        assert live_shell.list() == {}
        assert coding_session_owner.read(owner.key, owner.generation or "").status == (
            CodingSessionStatus.TERMINAL
        )
        assert not received.exists()
        return
    _active, remote, session = codex.start_generation(site, owner, request, _WAITS)

    assert session == CODEX_SESSION
    delivered = _wait_for_text(received, "continue from where you stopped")
    assert delivered.startswith("This session was interrupted")
    assert f"resume {CODEX_SESSION} --remote {remote}" in received.with_suffix(".argv").read_text()


def test_a_generation_that_ended_before_publishing_kills_the_codex_session(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, short_dir: Path
) -> None:
    received = short_dir / "r.txt"
    install_codex(unit_home, received)
    workspace = short_dir / "ws"
    workspace.mkdir()
    site = site_for(live_shell, short_dir, live_host)
    owner = _generation(site.cluster, workspace, supervised=False)
    assert owner.generation is not None
    coding_session_owner.terminate_generation(
        owner.key, owner.generation, reason="replaced", writer=AtomicWriter(SYSTEM_FS)
    )

    with pytest.raises(coding_session_owner.CodingSessionGenerationChangedError):
        codex.start_generation(site, owner, _request(workspace, _SKILL_DIR, prompt="brief"), _WAITS)

    assert live_shell.list() == {}
    assert not received.exists()


def test_a_supervisor_that_cannot_be_recorded_is_reclaimed_with_the_launch(
    live_shell: ShellSessions, live_host: SessionHost, unit_home: Path, short_dir: Path
) -> None:
    skill = _stub_skill(short_dir, short_dir / "supervisor.json")
    workspace = short_dir / "ws"
    workspace.mkdir()
    site = site_for(live_shell, short_dir, live_host)
    owner = _generation(site.cluster, workspace, supervised=True)
    assert owner.generation is not None
    coding_session_owner.terminate_generation(
        owner.key, owner.generation, reason="replaced", writer=AtomicWriter(SYSTEM_FS)
    )

    with pytest.raises(coding_session_owner.CodingSessionGenerationChangedError):
        codex.start_generation(site, owner, _request(workspace, skill), _WAITS)

    assert live_shell.list() == {}


@pytest.mark.parametrize(
    "extra",
    [
        ["--prompt", "briefing", "--tasks-file", "tasks.md"],
        ["--prompt", "briefing", "--work-file", "work.md"],
        ["--prompt", "   "],
    ],
)
def test_codex_prompt_cli_rejects_files_and_requires_non_empty_prompt(
    extra: list[str], tmp_path: Path
) -> None:
    with pytest.raises(SystemExit):
        spawn_codex.main([str(tmp_path), *extra])


def test_codex_prompt_rejects_resume(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        spawn_codex.main(
            [
                str(tmp_path),
                "--prompt",
                "briefing",
                "--resume",
                "01a0e1ac-adc7-7d33-bd13-8ce2c6a686c5",
            ]
        )


@pytest.mark.parametrize(
    ("status", "kwargs", "expected"),
    [
        ("DONE", {"status_is_current": True}, "collaboration-done"),
        ("HANDOFF", {"status_is_current": True}, "collaboration-handoff"),
        ("WORKING", {"owner_terminated": True}, "owner-terminated"),
        ("WORKING", {"session_crashed": True}, "session-crashed"),
        ("WORKING", {"expired": True}, "expired"),
    ],
)
def test_terminal_reason_covers_every_supervised_lifecycle(
    status: str, kwargs: dict[str, bool], expected: str
) -> None:
    inputs = {
        "status_is_current": False,
        "owner_terminated": False,
        "session_crashed": False,
        "expired": False,
        "work_file_deleted": False,
        "hard_limit_reached": False,
    }
    inputs.update(kwargs)

    assert (
        watch_work.terminal_reason(
            status,
            status_is_current=inputs["status_is_current"],
            owner_terminated=inputs["owner_terminated"],
            session_crashed=inputs["session_crashed"],
            expired=inputs["expired"],
            work_file_deleted=inputs["work_file_deleted"],
            hard_limit_reached=inputs["hard_limit_reached"],
        )
        == expected
    )


# meiva:note
# The gateway double's state: what the owner's status is and which notes were posted.
class _Cluster:
    def __init__(self, owner_status: AgentStatus | None) -> None:
        self.owner_status = owner_status
        self.notes: list[dict[str, object]] = []
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.method == "GET" and request.url.path == f"/api/agents/{_OWNER}":
            if self.owner_status is None:
                return httpx.Response(
                    404, json={"reason": ErrorReason.AGENT_NOT_FOUND.value, "detail": "no agent"}
                )
            return httpx.Response(200, json={"status": self.owner_status.value})
        if request.method == "POST" and request.url.path.endswith("/system-note"):
            self.notes.append(json.loads(request.content))
            return httpx.Response(200, json={"inbound_id": 1})
        raise AssertionError(f"unexpected gateway request {request.method} {request.url.path}")


def _launched(
    shell: ShellSessions,
    host: SessionHost,
    cluster: Path,
    workspace: Path,
    *,
    session_alive: bool,
    ttl_seconds: float = 3600,
) -> coding_session_owner.CodingSessionOwner:
    key = coding_session_owner.canonical_key(workspace, tool="codex", cluster=cluster)
    owner = _common.new_generation(
        key, owner_agent_id=_OWNER, tasks_file=None, work_file=None, ttl_seconds=ttl_seconds
    )
    assert owner.generation is not None and owner.expected_suffix is not None
    session_id = 99
    if session_alive:
        session_id, _full = shell.create(owner.expected_suffix, cwd=workspace, ttl=600.0, host=host)
    return coding_session_owner.publish_active(
        key,
        owner.generation,
        session_id=session_id,
        session_name=coding_session_owner.full_session_name(
            _OWNER, session_id, owner.expected_suffix
        ),
        writer=AtomicWriter(SYSTEM_FS),
    )


@pytest.mark.parametrize(
    ("status", "owner_status", "alive", "ttl", "expected"),
    [
        ("DONE", AgentStatus.RUNNING, True, 3600, "collaboration-done"),
        ("HANDOFF", AgentStatus.RUNNING, True, 3600, "collaboration-handoff"),
        ("WORKING", None, True, 3600, "owner-terminated"),
        ("WORKING", AgentStatus.RUNNING, False, 3600, "session-crashed"),
        ("WORKING", AgentStatus.RUNNING, True, 0.01, "expired"),
    ],
)
def test_canonical_watch_terminalizes_before_exit(
    status: str,
    owner_status: AgentStatus | None,
    alive: bool,
    ttl: float,
    expected: str,
    live_shell: ShellSessions,
    live_host: SessionHost,
    bound_agent: BindAgent,
    short_dir: Path,
) -> None:
    workspace = short_dir / "ws"
    workspace.mkdir()
    record = _launched(
        live_shell, live_host, short_dir / "c", workspace, session_alive=alive, ttl_seconds=ttl
    )
    assert record.generation is not None
    work_file = workspace / "work.md"
    work_file.write_text(f"STATUS: {status}\n")
    time.sleep(0.05)
    cluster = _Cluster(owner_status)

    with gateway_double(cluster) as clients:
        bound_agent(_OWNER, clients=clients)
        watch_work.watch(
            str(work_file),
            cluster=record.key.cluster,
            workspace=record.key.workspace,
            generation=record.generation,
            owner_agent_id=_OWNER,
        )

    final = coding_session_owner.read(record.key, record.generation)
    assert final.status == CodingSessionStatus.TERMINAL
    assert final.terminal_reason == expected
    if expected == "owner-terminated":
        assert cluster.notes == []
    else:
        (note,) = cluster.notes
        assert note["content"] == f"Codex generation {record.generation} terminalized ({expected})."
        assert note["resurrect"] is False
    assert live_shell.list() == {}


def test_canonical_notifications_never_resurrect_owner(bound_agent: BindAgent) -> None:
    cluster = _Cluster(AgentStatus.RUNNING)

    with gateway_double(cluster) as clients:
        bound_agent(_OWNER, clients=clients)
        assert watch_work._notify(_OWNER, "terminal", canonical=True)

    (request,) = cluster.requests
    assert request.url.path == f"/api/agents/{_OWNER}/system-note"
    body = json.loads(request.content)
    assert (body["content"], body["note_tag"], body["resurrect"]) == ("terminal", "task", False)
    keys = [value for value in request.headers.values() if _is_uuid(value)]
    assert len(keys) == 1


def test_a_generic_wake_is_a_peer_message(bound_agent: BindAgent) -> None:
    cluster = _Cluster(AgentStatus.RUNNING)

    def answer(request: httpx.Request) -> httpx.Response:
        cluster.requests.append(request)
        return httpx.Response(200, json={})

    with gateway_double(answer) as clients:
        bound_agent(_OWNER, clients=clients)
        assert watch_work._notify(_OWNER, "woken", canonical=False)

    (request,) = cluster.requests
    assert request.url.path == f"/api/agents/{_OWNER}/messages"


def _is_uuid(value: str) -> bool:
    try:
        return str(UUID(value)) == value
    except ValueError:
        return False
