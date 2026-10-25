from __future__ import annotations

import logging
import shlex
from collections.abc import Callable
from pathlib import Path

from base.native_process.os_platform import is_macos
from base.paths import logs_dir, meiva_home, run_dir
from base.sessions.helperproc import HelperProcSessionBackend, helper_client
from base.sessions.protocol import SessionBackend
from base.sessions.pty import client
from base.sessions.pty.keys import keys_to_bytes
from base.sessions.pty.paths import service_socket_path
from base.sessions.record import SessionRecord

_log = logging.getLogger(__name__)


class PosixProcSessionBackend(SessionBackend):
    @staticmethod
    def format_command(
        cmd: str,
        cwd: Path,
        *,
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> str:
        from base.sessions.env_forwarding import exec_into, venv_activation_prefix

        if not login_shell:
            return cmd
        body = exec_into(cmd) if exec_cmd else cmd
        inner = f"cd {cwd.as_posix()} && {venv_activation_prefix()}{body}"
        return f"exec bash -lc {shlex.quote(inner)}"

    def has_session(self, name: str) -> bool:
        from base.sessions import posixproc

        return posixproc.has_session(name)

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,
    ) -> bool:
        # meiva:note
        # Login-shell native sessions always exec the outer shell and exec the inner shell only when exec_cmd=True. They pass the caller's complete environment separately from argv. Without login_shell the command is forwarded unchanged.
        # meiva:link base/sessions/docs/session-backend.meiva.okf.md
        cmd = self.format_command(cmd, cwd, login_shell=login_shell, exec_cmd=exec_cmd)
        from base.sessions import posixproc

        return posixproc.new_session(name, cmd, cwd, env=env)

    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        # meiva:note
        # The native backend accepts the shared expected keyword without changing its supervisor's kill verdict. It has no separate force-escalation warning to quiet.
        # meiva:link base/sessions/docs/session-backend.meiva.okf.md
        del expected
        from base.sessions import posixproc

        return posixproc.kill_session(name, graceful=graceful, timeout=timeout)

    def graceful_signal(self, name: str, *, expected: SessionRecord | None = None) -> bool:
        from base.sessions import posixproc

        if expected is None:
            return posixproc.graceful_signal(name)
        return posixproc.graceful_signal(name, expected=expected)

    def list_sessions(self, prefix: str = "") -> list[str]:
        from base.sessions import posixproc

        return posixproc.list_sessions(prefix)

    def session_started_at(self, name: str) -> float | None:
        from base.sessions import posixproc

        return posixproc.session_started_at(name)

    def session_log_path(self, name: str) -> Path | None:
        from base.sessions import posixproc

        return posixproc.session_log_path(name)


class PtySessionBackend(SessionBackend):
    def __init__(self, config: client.PtyClientConfig) -> None:
        self._config = config
        self._socket = config.socket_path

    def has_session(self, name: str) -> bool:
        return client.has_session(name, socket_path=self._config.socket_path)

    def new_session(
        self,
        name: str,
        cmd: str,
        cwd: Path,
        *,
        env: dict[str, str],
        login_shell: bool = True,
        exec_cmd: bool = True,  # noqa: ARG002
    ) -> bool:
        # meiva:note
        # PTY sessions require a login shell, carry the child environment through the service request, and report service allocation refusal as False. The shared exec_cmd parameter is intentionally unused because an interactive shell stays alive.
        # meiva:link base/sessions/docs/session-backend.meiva.okf.md
        if not login_shell:
            raise NotImplementedError(f"{type(self).__name__} only creates login shells")
        try:
            client.create_session(name, str(cwd), env, cmd or None, config=self._config)
        except (client.ServiceError, client.ServiceUnavailableError) as exc:
            _log.warning("pty session allocation failed for %s: %s", name, exc)
            return False
        return True

    def service_available(self) -> bool:
        # meiva:note
        # The generation probe is the one question an absent service answers differently: `list` and `has` report an absent service as "no sessions", so a caller that read those would admit nothing and still call the service healthy. A live service always reports a generation.
        # meiva:link base/sessions/docs/session-backend.meiva.okf.md
        return client.host_generation(socket_path=self._config.socket_path) is not None

    def send(self, name: str, text: str) -> None:
        self._call("send", name, client.send, name, text.encode(), socket_path=self._socket)

    def send_keys(self, name: str, *keys: str) -> None:
        self._call(
            "send_keys", name, client.send, name, keys_to_bytes(keys), socket_path=self._socket
        )

    def capture_pane(self, name: str, lines: int = 200, *, scrollback: bool = True) -> str:
        return self._call(
            "capture",
            name,
            client.capture,
            name,
            lines,
            scrollback=scrollback,
            socket_path=self._socket,
        )

    @staticmethod
    def _call[T](op: str, name: str, call: Callable[..., T], *args: object, **kwargs: object) -> T:
        # meiva:link base/sessions/docs/session-backend.meiva.okf.md
        try:
            return call(*args, **kwargs)
        except (client.ServiceError, client.ServiceUnavailableError) as exc:
            raise RuntimeError(f"pty {op} {name!r} failed: {exc}") from exc

    def kill_session(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str]:
        ok, mode, _interrupted = self.kill_session_with_verdict(
            name, graceful=graceful, timeout=timeout, expected=expected
        )
        return ok, mode

    def kill_session_with_verdict(
        self,
        name: str,
        *,
        graceful: bool = False,
        timeout: float = 15.0,
        expected: bool = False,
    ) -> tuple[bool, str, bool]:
        # meiva:link base/sessions/docs/session-backend.meiva.okf.md
        del timeout, expected
        try:
            verdict = client.kill(name, graceful=graceful, socket_path=self._socket)
        except (client.ServiceError, client.ServiceUnavailableError) as exc:
            _log.warning("pty kill of %s failed: %s", name, exc)
            return False, "graceful" if graceful else "forced", True
        return True, verdict.mode, verdict.interrupted

    def list_sessions(self, prefix: str = "") -> list[str]:
        return [info.name for info in client.list_sessions(prefix, socket_path=self._socket)]

    def session_started_at(self, name: str) -> float | None:
        return self.session_started_ats([name])[name]

    def session_started_ats(self, names: list[str]) -> dict[str, float | None]:
        # meiva:note
        # A timestamp batch answers every requested name from one service listing; absent names map to None and an empty request returns an empty mapping.
        # meiva:link base/sessions/docs/session-backend.meiva.okf.md
        if not names:
            return {}
        started = {
            info.name: info.started_at for info in client.list_sessions(socket_path=self._socket)
        }
        return {name: started.get(name) for name in names}

    def session_generation(self, name: str) -> str | None:
        for info in client.list_sessions(name, socket_path=self._socket):
            if info.name == name:
                return info.generation
        return None

    # meiva:link base/sessions/docs/host-generation.meiva.okf.md
    def host_generation(self) -> str | None:
        return client.host_generation(socket_path=self._socket)

    def session_log_path(self, name: str) -> Path | None:
        return logs_dir() / f"{name}.out.log"


# meiva:link base/sessions/docs/session-backend.meiva.okf.md
def helper_spawn_enabled() -> bool:
    if not is_macos():
        return False
    try:
        from base.config import settings

        return bool(
            settings.services.permissions_helper_enabled
            and settings.services.permissions_helper_spawn
        )
    except Exception:
        _log.warning(
            "reading the permissions-helper settings failed; routing process creation "
            "through the legacy POSIX supervisor",
            exc_info=True,
        )
        return False


# meiva:link base/sessions/docs/session-backend.meiva.okf.md
def get_backend() -> SessionBackend:
    if helper_spawn_enabled():
        return HelperProcSessionBackend(helper_client)
    return PosixProcSessionBackend()


# meiva:link base/sessions/docs/session-backend.meiva.okf.md
def get_shell_backend() -> SessionBackend:
    # meiva:note
    # The shell backend of this process's own home: the ambient door the composition roots
    # (the gateway, the agent host, the shell SDK) call; a caller holding a home builds
    # `PtyClientConfig(socket_path=service_socket_path(run_dir(home)))` itself.
    return PtySessionBackend(
        client.PtyClientConfig(socket_path=service_socket_path(run_dir(meiva_home())))
    )
