# meiva:note
# Session environment projection: VIRTUAL_ENV follows the session working directory, and a
# watcher session carries the launcher's runner URLs only through its explicit override.


from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

import pytest

from base.host.env.dotenv_boot import project_watcher_runner_env
from base.sessions.env_forwarding import cwd_is_inside_checkout
from meiva.shell.session_env import SessionHost, session_environment

_RUNNER_DB = "postgresql://meiva_runner:runner@127.0.0.1:6433/meiva"
_OWNER_DB = "postgresql://meiva_main:owner@127.0.0.1:6433/meiva"
_RUNNER_REDIS = "redis://meiva:runner@127.0.0.1:6380/0"
_REFUSAL = "Launch the watcher from an agent-profile process"


def _environment(host: SessionHost, cwd: Path, **overrides: str) -> dict[str, str]:
    return session_environment(
        host,
        activate_venv=cwd_is_inside_checkout(cwd, host.checkout),
        agent_id=42,
        overrides=overrides or None,
    )


# meiva:note
# Worktree sessions retain PATH but cannot inherit another checkout's venv.
def test_session_environment_activates_only_checkout_cwds(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    inside = checkout / "nested"
    sibling_worktree = checkout / ".worktrees" / "feature"
    claude_sibling_worktree = checkout / ".claude" / "worktrees" / "feature"
    outside = tmp_path / "worktree"
    for directory in (inside, sibling_worktree, claude_sibling_worktree, outside):
        directory.mkdir(parents=True)
    host = SessionHost(
        checkout=checkout, forwarded={"PATH": "/venv/bin:/usr/bin", "VIRTUAL_ENV": "/venv"}
    )

    activated = _environment(host, inside)
    others = [
        _environment(host, cwd) for cwd in (sibling_worktree, claude_sibling_worktree, outside)
    ]

    assert activated["VIRTUAL_ENV"] == "/venv"
    assert all("VIRTUAL_ENV" not in env for env in others)
    assert all(env["PATH"] == "/venv/bin:/usr/bin" for env in [activated, *others])


def test_the_owner_id_rides_every_session_environment(tmp_path: Path) -> None:
    host = SessionHost(checkout=tmp_path, forwarded={"MEIVA_AGENT_ID": "7"})

    assert _environment(host, tmp_path)["MEIVA_AGENT_ID"] == "42"


# meiva:note
# The backend gets runner URLs only from the watcher's explicit override.
def test_runner_urls_reach_a_session_only_through_the_explicit_override(tmp_path: Path) -> None:
    host = SessionHost(checkout=tmp_path, forwarded={"PATH": "/usr/bin"})

    watcher_env = _environment(
        host, tmp_path, MEIVA_DB_URL=_RUNNER_DB, MEIVA_REDIS_URL=_RUNNER_REDIS
    )
    generic_env = _environment(host, tmp_path)

    assert watcher_env["MEIVA_DB_URL"] == _RUNNER_DB
    assert watcher_env["MEIVA_REDIS_URL"] == _RUNNER_REDIS
    assert "MEIVA_DB_URL" not in generic_env
    assert "MEIVA_REDIS_URL" not in generic_env


# meiva:note
# --- the launcher's projection (read from the environment of the launching process) ---


# meiva:note
# What the watcher's runner projection answers for exactly this environment, in `unit` as the home
# and `tmp_path` as the OS user's home.
def _projection(
    tmp_path: Path,
    env: Mapping[str, str],
    *,
    home: Path | None = None,
    serve_gateway: bool = True,
) -> dict[str, object]:
    unit = home if home is not None else tmp_path / ".meiva"
    unit.mkdir(parents=True, exist_ok=True)
    scenario_env = {
        "HOME": str(tmp_path),
        "MEIVA_HOME": str(unit),
        "MEIVA_CLUSTER_SECRET": "test-secret",
        **({"MEIVA_MACHINE_SERVE_GATEWAY": "true"} if serve_gateway else {}),
        **env,
    }
    try:
        projected = project_watcher_runner_env(scenario_env, home=unit, user_home=tmp_path)
    except RuntimeError as exc:
        return {"error": str(exc)}
    return {"env": projected}


@pytest.mark.parametrize("profile_key", ["MEIVA_PROCESS_PROFILE", "MEIVA_LAUNCHER_PROFILE"])
def test_agent_context_forwards_only_runner_urls(tmp_path: Path, profile_key: str) -> None:
    outcome = _projection(
        tmp_path,
        {"MEIVA_DB_URL": _RUNNER_DB, "MEIVA_REDIS_URL": _RUNNER_REDIS, profile_key: "agent"},
    )

    assert outcome == {"env": {"MEIVA_DB_URL": _RUNNER_DB, "MEIVA_REDIS_URL": _RUNNER_REDIS}}


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
    outcome = _projection(
        tmp_path,
        {
            "MEIVA_DB_URL": db_url,
            "MEIVA_REDIS_URL": redis_url,
            **({} if profile is None else {"MEIVA_PROCESS_PROFILE": profile}),
        },
    )

    assert _REFUSAL in str(outcome["error"])


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
    base = {
        "MEIVA_PROCESS_PROFILE": "agent",
        "MEIVA_CLUSTER_SECRET": "",
        "MEIVA_DB_URL": _RUNNER_DB,
    }
    unsafe = _projection(tmp_path, {**base, "MEIVA_REDIS_URL": redis_url})
    safe = _projection(tmp_path, {**base, "MEIVA_REDIS_URL": _RUNNER_REDIS})

    assert unsafe == {"env": {}}
    assert safe == {"env": {"MEIVA_DB_URL": _RUNNER_DB, "MEIVA_REDIS_URL": _RUNNER_REDIS}}


@pytest.mark.parametrize("different_home", [False, True])
def test_other_home_or_pure_runner_keeps_existing_child_boot(
    tmp_path: Path, different_home: bool
) -> None:
    outcome = _projection(
        tmp_path,
        {"MEIVA_DB_URL": _OWNER_DB, "MEIVA_REDIS_URL": _RUNNER_REDIS},
        home=tmp_path / "other-cluster" if different_home else None,
        serve_gateway=different_home,
    )

    assert outcome == {"env": {}}
