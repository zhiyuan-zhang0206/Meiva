"""Session VIRTUAL_ENV projection follows the session working directory."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.host.env import bootstrap, dotenv_boot
from base.host.env.tests.env_files import constant
from meiva.shell import sessions
from meiva.shell.tests.support import FakeDatabase
from meiva.tests.fixtures.pin_agent import pin_agent

_CHILD = Path(__file__).with_name("child_sdk_probes.py")


class _CapturingBackend:
    """Minimal session backend that records the environment handed to a shell."""

    def __init__(self) -> None:
        self.environments: list[dict[str, str]] = []

    def new_session(
        self,
        _name: str,
        _command: str,
        _cwd: Path,
        *,
        env: dict[str, str],
    ) -> bool:
        self.environments.append(env)
        return True


def _handle(backend: _CapturingBackend) -> sessions.ShellSessions:
    return sessions.ShellSessions(backend=backend, database=FakeDatabase(), agent_id=42)


def test_create_session_activates_only_checkout_cwds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Worktree sessions retain PATH but cannot inherit another checkout's venv."""

    checkout = tmp_path / "checkout"
    inside = checkout / "nested"
    sibling_worktree = checkout / ".worktrees" / "feature"
    claude_sibling_worktree = checkout / ".claude" / "worktrees" / "feature"
    outside = tmp_path / "worktree"
    inside.mkdir(parents=True)
    sibling_worktree.mkdir(parents=True)
    claude_sibling_worktree.mkdir(parents=True)
    outside.mkdir()
    backend = _CapturingBackend()
    activations: list[bool] = []
    handle = _handle(backend)
    monkeypatch.setattr(sessions, "repo_root", lambda: checkout)

    def forward(*, activate_venv: bool = True) -> dict[str, str]:
        activations.append(activate_venv)
        return {"PATH": "/venv/bin:/usr/bin", **({"VIRTUAL_ENV": "/venv"} if activate_venv else {})}

    monkeypatch.setattr(sessions, "forward_env_dict", forward)

    handle.create("inside", cwd=str(inside), ttl=120)
    handle.create("sibling", cwd=str(sibling_worktree), ttl=120)
    handle.create("claude-sibling", cwd=str(claude_sibling_worktree), ttl=120)
    handle.create("outside", cwd=str(outside), ttl=120)

    assert activations == [True, False, False, False]
    assert backend.environments[0]["VIRTUAL_ENV"] == "/venv"
    assert "VIRTUAL_ENV" not in backend.environments[1]
    assert "VIRTUAL_ENV" not in backend.environments[2]
    assert "VIRTUAL_ENV" not in backend.environments[3]
    assert backend.environments[1]["PATH"] == "/venv/bin:/usr/bin"


def test_watcher_override_reaches_backend_without_changing_generic_sessions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The backend gets runner URLs only from the watcher's explicit override."""
    from meiva import watcher

    runner_db = "postgresql://meiva_runner:runner@127.0.0.1:6433/meiva"
    runner_redis = "redis://meiva:runner@127.0.0.1:6380/0"
    monkeypatch.setenv("MEIVA_PROCESS_PROFILE", "agent")
    monkeypatch.delenv("MEIVA_LAUNCHER_PROFILE", raising=False)
    monkeypatch.setitem(os.environ, "MEIVA_DB_URL", runner_db)
    monkeypatch.setitem(os.environ, "MEIVA_REDIS_URL", runner_redis)
    monkeypatch.setitem(os.environ, "MEIVA_CLUSTER_SECRET", "test-secret")
    monkeypatch.setenv("MEIVA_HOME", str(Path.home() / ".meiva"))
    pin_agent(42)

    def workspace_for_test(_agent_id: int) -> Path:
        return tmp_path

    monkeypatch.setattr(sessions, "workspace_dir", workspace_for_test)
    monkeypatch.setattr(sessions, "repo_root", lambda: tmp_path / "checkout")
    backend = _CapturingBackend()
    monkeypatch.setattr(sessions, "shell_sessions", lambda: _handle(backend))

    class _CapturedSessionError(RuntimeError):
        pass

    original_new_session = backend.new_session

    def capture_then_stop(name: str, command: str, cwd: Path, *, env: dict[str, str]) -> bool:
        original_new_session(name, command, cwd, env=env)
        raise _CapturedSessionError

    monkeypatch.setattr(backend, "new_session", capture_then_stop)

    with pytest.raises(_CapturedSessionError):
        watcher.launch("pass", "10s", name="env-probe")
    watcher_env = backend.environments[0]
    assert watcher_env["MEIVA_DB_URL"] == runner_db
    assert watcher_env["MEIVA_REDIS_URL"] == runner_redis

    with pytest.raises(_CapturedSessionError):
        sessions.create_session("ordinary", cwd=str(tmp_path), ttl=120)
    generic_env = backend.environments[1]
    assert "MEIVA_DB_URL" not in generic_env
    assert "MEIVA_REDIS_URL" not in generic_env


def test_watcher_fail_fast_precedes_session_allocation(monkeypatch: pytest.MonkeyPatch) -> None:
    from meiva import watcher

    monkeypatch.setenv("MEIVA_HOME", str(Path.home() / ".meiva"))
    monkeypatch.setattr(bootstrap, "config_source_is_local", constant(True))
    monkeypatch.delenv("MEIVA_PROCESS_PROFILE", raising=False)
    monkeypatch.delenv("MEIVA_LAUNCHER_PROFILE", raising=False)
    monkeypatch.setitem(os.environ, "MEIVA_CLUSTER_SECRET", "test-secret")
    monkeypatch.setitem(
        os.environ, "MEIVA_DB_URL", "postgresql://meiva_main:owner@127.0.0.1:6433/meiva"
    )
    monkeypatch.setitem(os.environ, "MEIVA_REDIS_URL", "redis://meiva:runner@127.0.0.1:6380/0")
    pin_agent(42)

    def forbid_session(*_args: object, **_kwargs: object) -> tuple[int, str]:
        raise AssertionError("session allocation started")

    monkeypatch.setattr(sessions, "create_session", forbid_session)
    with pytest.raises(RuntimeError, match="Launch the watcher from an agent-profile process"):
        watcher.launch("pass", "10s", name="refused")


# Watcher runner projection at the session boundary.

_RUNNER_DB = "postgresql://meiva_runner:runner@127.0.0.1:6433/meiva"
_OWNER_DB = "postgresql://meiva_main:owner@127.0.0.1:6433/meiva"
_RUNNER_REDIS = "redis://meiva:runner@127.0.0.1:6380/0"


# meiva:note
# A gateway home that is the OS user's default home, and its launch environment: (user home, home, env).
def _secured_default_home(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    user_home = tmp_path / "user"
    home = user_home / ".meiva"
    home.mkdir(parents=True)
    env = {"MEIVA_MACHINE_SERVE_GATEWAY": "true", "MEIVA_CLUSTER_SECRET": "test-secret"}
    return user_home, home, env


@pytest.mark.parametrize("profile_key", ["MEIVA_PROCESS_PROFILE", "MEIVA_LAUNCHER_PROFILE"])
def test_agent_context_forwards_only_runner_urls(tmp_path: Path, profile_key: str) -> None:
    user_home, home, env = _secured_default_home(tmp_path)
    env |= {profile_key: "agent", "MEIVA_DB_URL": _RUNNER_DB, "MEIVA_REDIS_URL": _RUNNER_REDIS}

    assert dotenv_boot.project_watcher_runner_env(env, home=home, user_home=user_home) == {
        "MEIVA_DB_URL": _RUNNER_DB,
        "MEIVA_REDIS_URL": _RUNNER_REDIS,
    }


@pytest.mark.parametrize(
    ("profile", "db_url", "redis_url"),
    [
        (None, _RUNNER_DB, _RUNNER_REDIS),
        ("agent", _OWNER_DB, _RUNNER_REDIS),
        ("agent", "postgresql://[::1", _RUNNER_REDIS),
        ("agent", _RUNNER_DB, "redis://[::1"),
        ("agent", _RUNNER_DB, "redis://default:owner@127.0.0.1:6380/0"),
    ],
)
def test_secured_default_home_refuses_missing_or_unsafe_projection(
    tmp_path: Path, profile: str | None, db_url: str, redis_url: str
) -> None:
    user_home, home, env = _secured_default_home(tmp_path)
    env |= {"MEIVA_DB_URL": db_url, "MEIVA_REDIS_URL": redis_url}
    if profile is not None:
        env["MEIVA_PROCESS_PROFILE"] = profile

    with pytest.raises(RuntimeError, match="Launch the watcher from an agent-profile process"):
        dotenv_boot.project_watcher_runner_env(env, home=home, user_home=user_home)


@pytest.mark.parametrize(
    "redis_url",
    [
        "redis://127.0.0.1:6380/0",
        "redis://default:owner@127.0.0.1:6380/0",
        "redis://meiva@127.0.0.1:6380/0",
    ],
)
def test_no_secret_home_never_forwards_an_unauthenticated_or_admin_redis_url(
    tmp_path: Path, redis_url: str
) -> None:
    """Redis always authenticates, so an empty bearer does not relax the
    watcher projection: only the named runtime ACL URL with its password is
    forwarded, never the `default` admin user or a password-less URL."""
    user_home, home, env = _secured_default_home(tmp_path)
    env |= {
        "MEIVA_CLUSTER_SECRET": "",
        "MEIVA_PROCESS_PROFILE": "agent",
        "MEIVA_DB_URL": _RUNNER_DB,
        "MEIVA_REDIS_URL": redis_url,
    }

    assert dotenv_boot.project_watcher_runner_env(env, home=home, user_home=user_home) == {}
    env["MEIVA_REDIS_URL"] = _RUNNER_REDIS
    assert dotenv_boot.project_watcher_runner_env(env, home=home, user_home=user_home) == {
        "MEIVA_DB_URL": _RUNNER_DB,
        "MEIVA_REDIS_URL": _RUNNER_REDIS,
    }


@pytest.mark.parametrize("different_home", [False, True])
def test_other_home_or_pure_runner_keeps_existing_child_boot(
    tmp_path: Path, different_home: bool
) -> None:
    user_home, home, env = _secured_default_home(tmp_path)
    env |= {"MEIVA_DB_URL": _OWNER_DB, "MEIVA_REDIS_URL": _RUNNER_REDIS}
    if different_home:
        home = tmp_path / "other-cluster"
        home.mkdir()
    else:
        del env["MEIVA_MACHINE_SERVE_GATEWAY"]

    assert dotenv_boot.project_watcher_runner_env(env, home=home, user_home=user_home) == {}


def test_unprojected_agent_child_still_hits_owner_url_guard(
    tmp_path: Path,
) -> None:
    """A separate agent-profile interpreter still refuses an owner `.env` URL."""
    home = tmp_path / ".meiva"
    home.mkdir()
    (home / ".env").write_text(
        "\n".join(
            (
                f"MEIVA_DB_URL={_OWNER_DB}",
                f"MEIVA_REDIS_URL={_RUNNER_REDIS}",
                "MEIVA_CLUSTER_SECRET=test-secret",
                "MEIVA_MACHINE_SERVE_GATEWAY=true",
            )
        )
        + "\n"
    )
    env = os.environ.copy()
    env.update(
        {
            "HOME": str(tmp_path),
            "MEIVA_HOME": str(home),
            "MEIVA_PROCESS_PROFILE": "agent",
        }
    )
    for key in ("MEIVA_DB_URL", "MEIVA_REDIS_URL", "MEIVA_CLUSTER_SECRET", "VIRTUAL_ENV"):
        env.pop(key, None)
    child = subprocess.run(  # noqa: S603 — fixed interpreter and child script
        [sys.executable, str(_CHILD), "agent_profile_owner_url_guard"],
        env=env,
        cwd=Path(__file__).resolve().parents[3],
        capture_output=True,
        text=True,
        check=False,
    )
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "ValidationError True"
