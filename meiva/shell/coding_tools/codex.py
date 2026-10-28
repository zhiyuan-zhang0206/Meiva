"""Launch, inspect, or stop Codex sessions in persistent shells.

Every launch owns a generation of its own under ``(cluster, workspace,
codex)``; several may share a workspace, and a launch first reclaims its dead
siblings. Codex runs on the user's own ``~/.codex`` with per-session ``-c``
overrides, so every session stays resumable by the id the launch prints. A
supervised worker also starts an automatic lifecycle supervisor (the skill's
``watch_work.py``); a takeover (``impersonation_name``) runs file- and
supervisor-less with its briefing inlined in the launch message. A takeover
also wires the explicit shared app-server
topology: a private ``codex app-server --listen`` socket, the TUI connected to
it with ``--remote``, and the endpoint carried into the launch message so the
request records it (``--codex-remote``) and the relay delivers into the same
server.

The ``meiva-guide.external-agents`` skill's ``spawn_codex.py`` is the command-line
entry; it passes its own skill directory, whose ``references/`` holds the
collaboration contract (and locates the impersonator guide) and whose
``scripts/`` holds the supervisor script.
"""

from __future__ import annotations

import contextlib
import json
import shlex
import socket
import sys
from pathlib import Path

from base.agents.messages.external_caller import launch_caller_assignment
from base.clock import SYSTEM_TIMERS
from base.host.atomic_io import SYSTEM_FS, AtomicWriter
from base.host.env.agent_env import AGENT_ID_ENV
from base.lm.harness.codex.auth import SECRET_ENV_NAMES
from base.paths.expand import expand_home
from base.sessions import coding_session_owner
from meiva.shell.sessions import ShellSessions

from ._common import (
    LaunchSite,
    SessionGone,
    capability_report,
    capture_screen,
    cluster_home,
    cluster_sessions,
    init_file,
    kill_session_after_failed_launch,
    launch_site,
    new_generation,
    print_owner,
    terminate_generation_after_failed_launch,
    user_home,
    write_task_file_header,
)
from ._common import cancel as _cancel_generation
from ._common import resolve_dir as resolve_dir
from ._common import resolve_file as resolve_file
from ._common import status as _owner_status
from .plans.codex_screen import (
    dead_session_warning,
    is_working,
    parse_session_id,
    ready_timeout_line,
    ui_state,
)
from .plans.launch_request import LaunchRequest, checked_prompt, contract_file, worker_bootstrap
from .plans.launch_request import session_uuid as session_uuid
from .plans.pacing import CodexWaits, Pacing

# meiva:note
# Both codex processes this module starts — the shared app server (which does the model
# calls) and the TUI — otherwise inherit this agent's environment, where a cluster-pinned
# OPENAI_API_KEY outranks the ChatGPT login in `~/.codex/auth.json` and bills per token.
# `env -u` removes it rather than blanking it: the CLI asks whether the variable is
# present. The names come from the adapter's one list, not a second copy.
# meiva:link base/lm/harness/codex/docs/codex.meiva.okf.md
_DROP_CREDENTIALS: str = "env " + " ".join(f"-u {name}" for name in SECRET_ENV_NAMES) + " "

DEFAULT_TTL_SECONDS = 4 * 3600
_SUPERVISOR_TTL_PADDING_SECONDS = 300


def _config_overrides(workspace: Path) -> str:
    """Per-session ``-c`` overrides on top of the user's own ``~/.codex``.

    Codex runs on the default home exactly as a person's own terminal would, so
    its sessions stay resumable with ``codex resume``. The overrides touch no
    file: the workspace is trusted for this session only (the table replaces
    the ``projects`` map for this process), and the startup update check is
    off, since an unattended launch must never answer an "update now" prompt.
    """
    trusted = f'projects={{{json.dumps(workspace.as_posix())}={{trust_level="trusted"}}}}'
    return f"-c {shlex.quote(trusted)} -c check_for_update_on_startup=false"


# meiva:note
# Wait until Codex has finished MCP startup after publishing its handle. The TUI draws a frame while MCP is still starting, and a message sent then has its Enter swallowed, so "Starting MCP" counts as not ready until it disappears. A wait that runs out says so and lets the launch send anyway.
def wait_for_ready(shell: ShellSessions, sid: int, pacing: Pacing) -> None:
    print(f"waiting for session {sid} to be ready (MCP startup + render)...")
    deadline = SYSTEM_TIMERS.monotonic() + pacing.timeout
    rendered = False
    while SYSTEM_TIMERS.monotonic() < deadline:
        match capture_screen(shell, sid):
            case SessionGone(reason=reason):
                raise RuntimeError(f"Codex session {sid} ended before it was ready: {reason}")
            case str() as screen:
                state = ui_state(screen)
        if state != "blank":
            rendered = True
            SYSTEM_TIMERS.sleep(pacing.settle)
            if state == "ready":
                print("  -> ready (MCP startup finished)")
                return
            continue
        SYSTEM_TIMERS.sleep(pacing.poll)
    print(ready_timeout_line(pacing.timeout, rendered=rendered))


# meiva:note
# The visible screen, or None after saying so when the session has ended. A receipt checkpoint is loud, not fatal: the operator sees the warning and checks the session.
def _screen_or_warn(shell: ShellSessions, sid: int, action: str) -> str | None:
    screen = capture_screen(shell, sid)
    if isinstance(screen, SessionGone):
        print(dead_session_warning(action, screen.reason))
        return None
    return screen


# meiva:note
# The bootstrap message must actually submit, not park in the composer. The submission signal is "Working" on the screen. If it does not appear within the window, one Enter is sent (Enter submits a single-line queued message; the historical "Tab submits" note is wrong, 2026-09-03 #5779 re-test) and the screen is checked again. Kept loud but not fatal: a dead session's refusal warns and returns, because an escaped ValueError would roll the launch back (the caller kills the session and terminates the generation on exceptions).
def verify_submitted(shell: ShellSessions, sid: int, pacing: Pacing) -> None:
    print("verifying the bootstrap message was submitted...")
    deadline = SYSTEM_TIMERS.monotonic() + pacing.timeout
    while SYSTEM_TIMERS.monotonic() < deadline:
        output = _screen_or_warn(shell, sid, "capture")
        if output is None:
            return
        if is_working(output):
            print("  -> submitted (Working visible)")
            return
        SYSTEM_TIMERS.sleep(pacing.poll)
    print("  -> not submitted within window; sending Enter once")
    try:
        shell.send_keys(sid, "Enter")
    except ValueError as exc:
        print(dead_session_warning("Enter retry", str(exc)))
        return
    SYSTEM_TIMERS.sleep(pacing.settle)
    output = _screen_or_warn(shell, sid, "capture")
    if output is None:
        return
    if is_working(output):
        print("  -> submitted after Enter retry")
    else:
        print(
            "  -> WARNING: still no Working signal. The contract message may be "
            "parked in the composer ('tab to queue message'). Check the session "
            "and press Enter manually; see codex-tui-first-message trap in memory."
        )


# meiva:note
# The Codex session id, read from the `/status` card of the ready TUI. It is the id `codex resume <id>` takes, and what a person sees in the same card. Sent before the first message, so the composer is empty; a `/status` that parked gets one Enter. No id means the launch cannot be resumed later, so it fails.
def read_session_id(shell: ShellSessions, sid: int, pacing: Pacing) -> str:
    shell.send(sid, "/status")
    for attempt in range(2):
        deadline = SYSTEM_TIMERS.monotonic() + pacing.timeout / 2
        while SYSTEM_TIMERS.monotonic() < deadline:
            found = parse_session_id(shell.capture(sid, 80, scrollback=True))
            if found is not None:
                print(f"  -> codex session {found}")
                return found
            SYSTEM_TIMERS.sleep(pacing.poll)
        if attempt == 0:
            shell.send_keys(sid, "Enter")
    raise RuntimeError(
        f"could not read the Codex session id from /status in session {sid}; "
        "the launch is rolled back because it could not be resumed later"
    )


def supervisor_code(owner: coding_session_owner.CodingSessionOwner, watcher: Path) -> str:
    """Run the skill's supervisor ``watch`` for exactly this generation."""
    if owner.generation is None or owner.owner_agent_id is None or owner.work_file is None:
        raise RuntimeError("launching owner is missing supervisor inputs")
    return (
        "import runpy\n"
        f"_watch = runpy.run_path({str(watcher)!r})['watch']\n"
        f"_watch({str(owner.work_file)!r}, cluster={owner.key.cluster!r}, "
        f"workspace={owner.key.workspace!r}, generation={owner.generation!r}, "
        f"owner_agent_id={owner.owner_agent_id!r})\n"
    )


def supervisor_command(owner: coding_session_owner.CodingSessionOwner, watcher: Path) -> str:
    """The supervisor PTY's command, with the owner's identity pinned in its environment."""
    code = supervisor_code(owner, watcher)
    return (
        f"exec env {AGENT_ID_ENV}={owner.owner_agent_id} "
        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
    )


def supervisor_name(owner: coding_session_owner.CodingSessionOwner) -> str:
    if owner.generation is None:
        raise RuntimeError("launching owner has no generation")
    return coding_session_owner.supervisor_suffix(owner.key, owner.generation)


# meiva:note
# Launch a quiet PTY supervisor that cannot resurrect a terminated owner. `command` is what the supervisor session runs (`supervisor_command`), built by the caller so a launch that cannot supervise fails before any session exists.
def launch_supervisor(
    site: LaunchSite,
    owner: coding_session_owner.CodingSessionOwner,
    ttl_seconds: float,
    command: str,
) -> tuple[int, str]:
    if owner.owner_agent_id is None:
        raise RuntimeError("launching owner has no agent identity")
    supervisor_ttl = min(ttl_seconds + _SUPERVISOR_TTL_PADDING_SECONDS, 86_400)
    suffix = supervisor_name(owner)
    session_id = site.open_session(suffix, supervisor_ttl)
    try:
        site.shell.send(session_id, command)
    except BaseException:
        kill_session_after_failed_launch(site.shell, session_id)
        raise
    return session_id, coding_session_owner.full_session_name(
        owner.owner_agent_id, session_id, suffix
    )


def app_server_endpoint(key: coding_session_owner.CodingSessionKey, generation: str) -> str:
    """The single endpoint both the TUI and the codex relay connect to.

    The socket lives in a short per-user directory that
    ``codex_app_server_socket`` creates and verifies (the mkdir below is then a
    no-op); the app server binds the socket itself.
    """
    socket_path = coding_session_owner.codex_app_server_socket(key, generation)
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    return f"unix://{socket_path}"


def app_server_command(
    owner: coding_session_owner.CodingSessionOwner,
    workspace: Path,
    endpoint: str,
    caller_instance: str | None = None,
) -> str:
    """Start the shared app server plus the janitor that outlives the kill rounds.

    The kill path signals the shell's and the foreground process group only, so
    a background child in its own group survives it; the janitor is that child's
    cleanup owner — it watches the session child (this shell, then the exec'd
    TUI) and ends the server when it dies, then removes the socket. The
    hands-off policy is configured on the server itself: the remote TUI's flags
    do not reach the server's tools.

    The janitor's cadence bounds are fixed launch constants, not tuning knobs:
    a 2s dead-check interval reaps within a few seconds of session death at one
    cheap sleep, and a 1s grace lets the server exit on SIGTERM (and unlink its
    socket) before the SIGKILL.
    """
    from base.agents.messages.external_caller import launch_caller_assignment

    if owner.state_dir is None:
        raise RuntimeError("launching owner has no generation state directory")
    log_path = _app_server_log_path(owner)
    socket_path = endpoint.removeprefix("unix://")
    janitor = (
        "{ while kill -0 $$ 2>/dev/null && kill -0 $AP 2>/dev/null; do sleep 2; done; "
        "if ! kill -0 $$ 2>/dev/null; then "
        "if ps -p $AP -o command= 2>/dev/null | grep -q 'app-server'; then "
        "kill $AP 2>/dev/null; sleep 1; kill -9 $AP 2>/dev/null; fi; fi; "
        f"rm -f {shlex.quote(socket_path)}; }}"
        " &"
    )
    # The server pid travels as ${!}: interactive panes run with history
    # expansion, where a bare $! can abort the whole line (review C1); ${!} is
    # accepted by bash, dash and zsh alike, so the swap carries no behavior
    # risk (review N8).
    return (
        f"(cd {shlex.quote(workspace.as_posix())} && "
        f"{launch_caller_assignment('codex', caller_instance)}"
        f"exec {_DROP_CREDENTIALS}codex app-server --listen {shlex.quote(endpoint)}"
        ' -c approval_policy="never" -c sandbox_mode="danger-full-access"'
        f" {_config_overrides(workspace)}"
        f" > {shlex.quote(str(log_path))} 2>&1) & AP=${{!}}; "
        f"{janitor}"
    )


def _app_server_log_path(owner: coding_session_owner.CodingSessionOwner) -> Path:
    """The shared app server's stdout/stderr log inside the owner's state dir."""
    assert owner.state_dir is not None  # noqa: S101 — callers run after the launch-field check
    return owner.state_dir / "app-server.log"


# meiva:note
# Block until the shared app server accepts a connection on its socket. Socket acceptance is the readiness fact (the app server writes no startup line); a launch whose endpoint never answers fails loudly instead of starting a TUI that queues nothing. `log_path`, when given, names the server's own log in the timeout error so a failing launch points at the evidence (review N3). The connect probe is bounded at the house 2 seconds for same-machine sockets, as in the PTY launcher.
def wait_for_app_server(endpoint: str, pacing: Pacing, *, log_path: Path | None = None) -> None:
    path = endpoint.removeprefix("unix://")
    print(f"waiting for the shared codex app server at {endpoint}...")
    deadline = SYSTEM_TIMERS.monotonic() + pacing.timeout
    while SYSTEM_TIMERS.monotonic() < deadline:
        if Path(path).exists():
            with contextlib.suppress(OSError):
                probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                probe.settimeout(2.0)
                try:
                    probe.connect(path)
                finally:
                    probe.close()
                print("  -> app server ready")
                return
        SYSTEM_TIMERS.sleep(pacing.poll)
    detail = f" (app-server log: {log_path})" if log_path is not None else ""
    raise RuntimeError(
        f"the shared codex app server did not become ready at {endpoint}{detail}; "
        "check that the installed codex supports `app-server --listen` and `--remote` "
        "(see canonical_codex_owner.md) — the takeover launch is refused without it"
    )


def codex_command(
    workspace: Path,
    caller_instance: str | None = None,
    remote: str | None = None,
    *,
    resume: str | None = None,
) -> str:
    """Build the interactive TUI command; a takeover clears the screen first.

    The shared app server line has already filled the screen with its echo,
    which would otherwise pass for a rendered TUI frame and let the launch
    message park in the composer (the codex-tui-first-message trap). With
    ``resume`` the TUI reopens that recorded session (``codex resume <id>``)
    instead of starting a new one.
    """
    from base.agents.messages.external_caller import launch_caller_assignment

    prefix = "clear && " if remote is not None else ""
    subcommand = f"resume {shlex.quote(resume)} " if resume is not None else ""
    remote_flag = f"--remote {shlex.quote(remote)} " if remote is not None else ""
    return (
        f"{prefix}cd {shlex.quote(workspace.as_posix())} && "
        f"{launch_caller_assignment('codex', caller_instance)}"
        f"exec {_DROP_CREDENTIALS}codex {subcommand}{remote_flag}--dangerously-bypass-approvals-and-sandbox "
        f"{_config_overrides(workspace)}"
    )


def status(workspace: Path, *, mode: str = "auto", force: bool = False) -> int:
    """Print every generation recorded for the workspace."""
    return _owner_status(
        coding_session_owner.canonical_key(
            expand_home(workspace, user_home=user_home()), tool="codex", cluster=cluster_home()
        ),
        capability=capability_report("codex", mode=mode, force=force),
    )


def cancel(workspace: Path, generation: str) -> int:
    """Stop and terminalize exactly this generation."""
    return _cancel_generation(
        coding_session_owner.canonical_key(
            expand_home(workspace, user_home=user_home()), tool="codex", cluster=cluster_home()
        ),
        generation,
        sessions=cluster_sessions(),
    )


# meiva:note
# Start the shared app-server and Codex TUI in the published PTY and deliver the launch message. Returns the shared app-server endpoint and the Codex session id, which `codex resume` and `meiva impersonate request --provider codex` take.
def start_codex(
    site: LaunchSite,
    sid: int,
    owner: coding_session_owner.CodingSessionOwner,
    request: LaunchRequest,
    waits: CodexWaits,
) -> tuple[str, str]:
    if owner.generation is None:
        raise RuntimeError("launching owner has no generation")
    shell = site.shell
    remote = app_server_endpoint(owner.key, owner.generation)
    shell.send(sid, app_server_command(owner, request.workspace, remote, request.caller_instance))
    wait_for_app_server(remote, waits.server, log_path=_app_server_log_path(owner))
    shell.send(
        sid,
        codex_command(
            request.workspace, request.caller_instance, remote=remote, resume=request.resume
        ),
    )
    wait_for_ready(shell, sid, waits.ready)
    codex_session = read_session_id(shell, sid, waits.status)
    if request.resume is not None and codex_session != request.resume:
        raise RuntimeError(
            f"codex reopened session {codex_session}, not the requested {request.resume}"
        )
    if request.prompt is not None:
        message = request.prompt
    else:
        assert request.tasks_file is not None and request.work_file is not None  # noqa: S101
        message = worker_bootstrap(
            contract_file(request.skill_dir),
            request.workspace,
            request.tasks_file,
            request.work_file,
            resumed=request.resume is not None,
            provenance=request.provenance,
        )
    shell.send(sid, message)
    verify_submitted(shell, sid, waits.submit)
    return remote, codex_session


# meiva:note
# Supervise, publish and start one fresh generation; roll back on failure. A supervised launch starts its supervisor first and records it; a prompt launch (a takeover) has none. The coding session is published as active before the tool starts, so a generation that ended meanwhile (its record no longer launching) fails the launch here. Every session the launch opened (the supervisor included, even when recording it failed) is reclaimed on failure.
def start_generation(
    site: LaunchSite,
    owner: coding_session_owner.CodingSessionOwner,
    request: LaunchRequest,
    waits: CodexWaits,
) -> tuple[coding_session_owner.CodingSessionOwner, str, str]:
    if (
        owner.generation is None
        or owner.expected_suffix is None
        or owner.state_dir is None
        or owner.owner_agent_id is None
    ):
        raise RuntimeError("new owner generation is missing launch fields")
    key = owner.key
    generation = owner.generation
    expected_suffix = owner.expected_suffix
    owner_agent_id = owner.owner_agent_id
    opened: list[int] = []
    try:
        owner.state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if request.prompt is None:
            command = supervisor_command(owner, request.skill_dir / "scripts" / "watch_work.py")
            watcher_id, watcher_name = launch_supervisor(site, owner, request.ttl_seconds, command)
            opened.append(watcher_id)
            owner = coding_session_owner.attach_supervisor(
                key,
                generation,
                session_id=watcher_id,
                session_name=watcher_name,
                writer=AtomicWriter(SYSTEM_FS),
            )
        sid = site.open_session(expected_suffix, request.ttl_seconds)
        opened.append(sid)
        full_name = coding_session_owner.full_session_name(owner_agent_id, sid, expected_suffix)
        active = coding_session_owner.publish_active(
            key,
            generation,
            session_id=sid,
            session_name=full_name,
            writer=AtomicWriter(SYSTEM_FS),
        )
        remote, codex_session = start_codex(site, sid, owner, request, waits)
    except BaseException:
        for session_id in opened:
            kill_session_after_failed_launch(site.shell, session_id)
        terminate_generation_after_failed_launch(key, generation, sessions=site.shell.backend)
        raise
    return active, remote, codex_session


def launch(
    workspace: Path,
    tasks_file: Path | None,
    work_file: Path | None,
    ttl_seconds: float,
    caller_instance: str | None = None,
    *,
    skill_dir: Path,
    resume: str | None = None,
    prompt: str | None = None,
    provenance: str | None = None,
) -> int:
    """Launch a Codex session in a persistent shell with a shared app server.

    When ``prompt`` is given, sends that initial prompt without creating
    task/work files or a supervisor. Otherwise launches a supervised worker with
    ``tasks_file``, ``work_file``, and ``watch_work.py``. Every launch starts a
    shared ``codex app-server --listen`` endpoint and prints both
    ``codex_app_server`` and ``codex_session`` so the session can also accept
    an impersonation handover when instructed via a message.
    """
    request = LaunchRequest(
        workspace,
        tasks_file,
        work_file,
        ttl_seconds,
        caller_instance,
        checked_prompt(prompt, tasks_file, work_file),
        skill_dir,
        resume,
        provenance,
    )
    launch_caller_assignment("codex", caller_instance)
    site = launch_site()
    if tasks_file is not None and work_file is not None:
        init_file(tasks_file, "")
        init_file(work_file, "STATUS: WORKING\n\n## Log\n\n")
        write_task_file_header(tasks_file, provenance or "")
    key = coding_session_owner.canonical_key(
        expand_home(workspace, user_home=user_home()), tool="codex", cluster=site.cluster
    )
    owner = new_generation(
        key,
        owner_agent_id=site.shell.agent_id,
        tasks_file=tasks_file,
        work_file=work_file,
        ttl_seconds=ttl_seconds,
        sessions=site.shell.backend,
    )
    active, remote, codex_session = start_generation(site, owner, request, CodexWaits())
    print(f"ready. name={active.expected_suffix} workspace={workspace}")
    print(f"codex_app_server={remote}")
    print_owner(active, "codex")
    print(f"codex_session={codex_session}")
    return 0
