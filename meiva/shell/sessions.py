"""Persistent shell sessions that preserve cwd, env, and background processes across calls."""

__all_for_meiva__ = ["capture", "kill", "list", "new", "renew", "send", "send_keys"]

import builtins
import contextlib
from datetime import datetime
from pathlib import Path
from typing import cast

from base.agents.context.clients import DatabaseHandle
from base.cluster import session_name
from base.log import logger
from base.paths import repo_root, workspace_dir
from base.sessions.backend import PtySessionBackend, get_shell_backend
from base.sessions.env_forwarding import cwd_is_inside_checkout, forward_env_dict
from base.sessions.page_session import is_page_label
from meiva.sdk_surface.validation import coerce_str, coerce_typed
from meiva.security import scan_content
from meiva.shell import session_env, session_plan
from meiva.shell.handle import SessionHandle
from meiva.shell.session_env import SessionHost
from meiva.shell.session_plan import validate_ttl


# meiva:note
# One agent's persistent shell sessions on a backend and a database. The module functions below are the SDK entries: each reads this process's agent identity once (`shell_sessions()`) and calls the matching method here, whose every dependency is a constructor or call argument, so a test builds one over the real pty-sessions backend and a throwaway database.
class ShellSessions:
    def __init__(
        self, *, backend: PtySessionBackend, database: DatabaseHandle, agent_id: int
    ) -> None:
        self._backend = backend
        self._database = database
        self._agent_id = agent_id

    @property
    def agent_id(self) -> int:
        return self._agent_id

    # meiva:note
    # Every shell session of this agent, watchers included, is named `meiva-agent-<agent_id>-shell-<session_id>[-<name>]`; kill_all and `list` filter on this prefix.
    def shell_prefix(self) -> str:
        return f"{session_name(f'agent-{self._agent_id}')}-shell-"

    def _names(self) -> builtins.list[str]:
        return self._backend.list_sessions()

    def resolve(self, session_id: int) -> str:
        return session_plan.match_session(self._names(), self.shell_prefix(), session_id)

    # meiva:note
    # `ttl` is required: user ruling 2026-08-27 made shell TTL mandatory (the idle-shell-reminder daemon is gone and TTL is the only reclamation mechanism). Every caller, shells and watchers alike, records its deadline in `agent_shell_ttls`; for a watcher that deadline IS the watcher's target (launch timeout, cron end, at moment plus grace; user ruling 2026-09-14, task #3411). The cap is applied here, at the write point, so every caller is bound by it. The id is allocated before the host-level PTY admission gate: during an operator freeze a refused attempt leaves a harmless gap in this monotonic per-agent sequence, which is never rolled back or reused, so an old numeric handle stays stale instead of naming a later session. A session whose deadline row cannot be written is disposed of, because the reaper could never see it.
    def create(
        self,
        name: str | None,
        *,
        cwd: Path,
        ttl: float,
        system: bool = False,
        env_overrides: dict[str, str] | None = None,
        host: SessionHost,
    ) -> tuple[int, str]:
        from meiva.shell import ttl_store

        session_plan.validate_name(name, page_label=is_page_label(name))
        ttl = validate_ttl(ttl, system=system)
        session_id = ttl_store.next_session_index(self._database, self._agent_id)
        full = session_plan.session_full_name(self.shell_prefix(), session_id, name)
        env = session_env.session_environment(
            host,
            activate_venv=cwd_is_inside_checkout(cwd, host.checkout),
            agent_id=self._agent_id,
            overrides=env_overrides,
        )
        if not self._backend.new_session(full, "", cwd, env=env):
            raise RuntimeError(f"failed to create session {full!r}")
        try:
            ttl_store.record_ttl(self._database, self._agent_id, session_id, ttl)
        except RuntimeError:
            try:
                self._backend.kill_session(full, graceful=False)
            except Exception:
                logger.opt(exception=True).warning(
                    "disposing of untracked session {!r} after its TTL record failed also failed; "
                    "the session stays live",
                    full,
                )
            raise
        return session_id, full

    # meiva:note
    # Text and Enter go in separate writes: a combined write races TUI programs (Claude Code, Codex) that are still processing the typed text when Enter arrives.
    def send(self, session_id: int, cmd: str, *, enter: bool = True) -> None:
        target = self.resolve(session_id)
        self._backend.send(target, cmd)
        if enter:
            self._backend.send_keys(target, "Enter")

    def send_keys(self, session_id: int, *keys: str) -> None:
        self._backend.send_keys(self.resolve(session_id), *keys)

    # meiva:note
    # A session holds whatever ran in it, an interactive fetch or a coding agent rendering a web page, so reading one ingests exactly as `shell.run` does. The text is scanned for the same reason and comes back byte-for-byte.
    def capture(self, session_id: int, lines: int, *, scrollback: bool) -> str:
        name = self.resolve(session_id)
        pane = self._backend.capture_pane(name, lines, scrollback=scrollback)
        return scan_content(pane, source="shell.sessions.capture")

    def kill(self, session_id: int) -> None:
        ok, _mode = self._backend.kill_session(self.resolve(session_id), graceful=False)
        if not ok:
            raise RuntimeError(f"failed to kill session {session_id}")

    def kill_all(self) -> int:
        names = session_plan.own_names(self._names(), self.shell_prefix())
        for name in names:
            with contextlib.suppress(RuntimeError):
                self._backend.kill_session(name, graceful=False)
        return len(names)

    # meiva:note
    # Extends the deadline row and writes the audit trail. The write UPDATEs the deadline in one transaction guarded by `expires_at > clock_timestamp()`: an expired row is never renewable, which is what makes the pair with the reaper airtight, since a successful renewal proves the row was unexpired at its write and a renewal attempted after the deadline loses. The audit trail is one `agent_shell_ttl_renewals` row (requested ttl plus before and after deadlines) in the same transaction, the `renewals` counter and `last_renewed_at` on the main row, and a telemetry `shell_ttl_renewed` event. The before/after read and the write use separate connections on purpose: the guarded UPDATE re-checks the deadline itself, so the read needs no lock, and a concurrent second renewal can at worst stamp a slightly stale `prev_expires_at`. A session that is not this agent's, or not alive, raises ValueError like send and capture.
    def renew(self, session_id: int, ttl: float) -> datetime:
        from base import telemetry
        from meiva.shell import ttl_store

        self.resolve(session_id)
        prev_expires = ttl_store.read_expiry(self._database, self._agent_id, session_id)
        if prev_expires is None:
            raise RuntimeError(
                f"session {session_id} has no TTL row — a pre-mandate session cannot be renewed"
            )
        new_expires = ttl_store.apply_renewal(
            self._database, self._agent_id, session_id, ttl, prev_expires
        )
        telemetry.emit(
            "telemetry",
            "shell_ttl_renewed",
            level="info",
            agent_id=self._agent_id,
            attributes={
                "session_id": session_id,
                "ttl_s": round(ttl),
                "prev_expires_at": prev_expires.isoformat(),
                "new_expires_at": new_expires.isoformat(),
            },
        )
        return new_expires

    def list(self) -> builtins.dict[int, str | None]:
        return session_plan.session_table(self._names(), self.shell_prefix())


# meiva:note
# The machine facts a new session starts from, read once per creation: this checkout and the environment the session forwards.
def session_host() -> SessionHost:
    return SessionHost(checkout=repo_root(), forwarded=forward_env_dict(activate_venv=True))


# meiva:note
# The sessions of one agent on this process's session backend and cluster database (the same SDK root as `database()` and `bus()`). The backend is the pty-sessions service; `get_shell_backend` answers its client typed as the abstract backend, and the shell operations live on the concrete one.
def open_sessions(agent_id: int) -> ShellSessions:
    from meiva.sdk_surface.settings import database

    return ShellSessions(
        backend=cast(PtySessionBackend, get_shell_backend()),
        database=database(),
        agent_id=agent_id,
    )


# meiva:note
# Raises RuntimeError when this process has no agent identity: shell sessions are an agent's, so a standalone script that imports meiva gets an explicit refusal, never another agent's or a global's sessions.
def shell_sessions() -> ShellSessions:
    from meiva.sdk_surface import agent_identity

    agent_id = agent_identity.agent_id()
    if agent_id is None:
        raise RuntimeError(
            "Cannot use shell sessions: this process has no agent identity. "
            "meiva.shell.sessions requires an agent process or a background "
            "script launched by one (which derives the identity from "
            "MEIVA_AGENT_ID). Running a standalone script that imports meiva "
            "does not set an agent identity."
        )
    return open_sessions(agent_id)


# meiva:note
# Allocate the next session id and create the shell session. Internal: `new()` (shell), `meiva.watcher.spawn.run` (named "watcher") and the background runs use it. `cwd` defaults to the agent's workspace, the same base `meiva.shell.run` uses.
def create_session(
    name: str | None = None,
    *,
    cwd: str | None = None,
    ttl: float,
    system: bool = False,
    env_overrides: dict[str, str] | None = None,
) -> tuple[int, str]:
    shell = shell_sessions()
    start = Path(cwd) if cwd is not None else workspace_dir(shell.agent_id)
    return shell.create(
        name, cwd=start, ttl=ttl, system=system, env_overrides=env_overrides, host=session_host()
    )


def new(name: str, *, ttl: float) -> int:
    """`name` is only a display label — a lowercase slug like `"dev-server"`;
    every operation takes the id. Names starting with `page-` are reserved for
    pages opened with `meiva.ui.serve`.

    Args:
        ttl: same semantics as `run_background` — required hard lifetime in
            seconds from creation; a deadline-bound task belongs in
            `meiva.watcher` instead. Extend the deadline later, before it
            passes, with `renew(id, ttl=)`."""
    name = coerce_str(name, "name")
    ttl = coerce_typed(ttl, "ttl", (int, float))
    shell = shell_sessions()
    session_id, _ = shell.create(
        name, cwd=workspace_dir(shell.agent_id), ttl=ttl, host=session_host()
    )
    return session_id


# `id` / `list` shadow builtins intentionally: these are the agent-facing names.
# (flake8-builtins `A` is not in this repo's ruff select, so no noqa is needed.)
def send(id: int | SessionHandle, cmd: str, *, enter: bool = True) -> None:
    """Send text asynchronously, without waiting for the command to finish.

    Set `enter=False` to type without submitting. Terminal applications may
    truncate long input. For long instructions, use the receiver's supported
    paste mode or send a short pointer to a file it can read."""
    id = _session_id(id)
    cmd = coerce_str(cmd, "cmd")
    enter = coerce_typed(enter, "enter", bool)
    shell_sessions().send(id, cmd, enter=enter)


def send_keys(id: int | SessionHandle, *keys: str) -> None:
    """Send raw keys to a session without submitting a line. Each argument is
    one key: a single character, or a name like `C-c`, `Escape`, `Up`,
    `Enter`, `Space`, `PageUp`."""
    id = _session_id(id)
    keys = tuple(coerce_str(key, "key") for key in keys)
    shell_sessions().send_keys(id, *keys)


def capture(id: int | SessionHandle, lines: int | None = None, *, scrollback: bool = True) -> str:
    """The session's most recent `lines` of output, including history that
    has scrolled past. Omit `lines` for the configured default
    (``display.shell_capture_default_lines``, 200 out of the box). Pass
    `scrollback=False` to get only the current visible screen instead —
    needed for full-screen programs that redraw in place (`lines` is
    ignored then)."""
    from meiva.sdk_surface.settings import config_authority

    id = _session_id(id)
    resolved_lines: int
    if lines is None:
        resolved_lines = config_authority().service_field_value("shell_capture_default_lines")
    else:
        resolved_lines = coerce_typed(lines, "lines", int)
    scrollback = coerce_typed(scrollback, "scrollback", bool)
    return shell_sessions().capture(id, resolved_lines, scrollback=scrollback)


def _session_id(id: object) -> int:
    """Accept a session id or the handle `run_background`/`watcher.*` returned.

    A watcher and a background command both come back as `SessionHandle`; passing
    that value straight to `sessions` must keep working, so the handle's id wins
    over the strict int check.
    """
    if isinstance(id, SessionHandle):
        return id.session_id
    return coerce_typed(id, "id", int)


def kill(id: int | SessionHandle) -> None:
    id = _session_id(id)
    shell_sessions().kill(id)


# Not in __all_for_meiva__, so never rendered into the SDK docs: a prefix-scoped
# cleanup helper used by test fixtures. The agent exit path deliberately does
# NOT reap sessions — they outlive the turn (see services/agent_runner/agent_host/host.py); the agent
# itself kills sessions one by one via kill(id).
def kill_all() -> int:
    return shell_sessions().kill_all()


def renew(id: int | SessionHandle, *, ttl: float) -> datetime:
    """Move a live session's expiry to now + `ttl` seconds.

    Each renewal accepts at most 24 hours and replaces, rather than extends, the
    existing deadline. Only your own unexpired sessions can be renewed, including
    watcher sessions. Renewal does not change a watcher's scheduled end or timeout;
    renewing a longer-lived session can shorten its expiry.

    Returns:
        The new expiry time."""
    id = _session_id(id)
    ttl = validate_ttl(coerce_typed(ttl, "ttl", (int, float)))
    return shell_sessions().renew(id, ttl)


def list() -> dict[int, str | None]:
    """Your sessions: id -> display name (None for unnamed).

    Entries named `page-<name>` are live page servers for pages this agent
    opened via `meiva.ui.serve`, not leftovers; see `meiva.ui` for their lifecycle.
    """
    return shell_sessions().list()
