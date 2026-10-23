# meiva:note
# CLI launcher context (#4334): the entry pop records, the gates read it.
#
# Split out of test_dotenv_boot.py to stay under the structure-lint's per-file
# line budget.
#
# cli.main's entry pops MEIVA_PROCESS_PROFILE (settings-full by design) and
# records the popped value as MEIVA_LAUNCHER_PROFILE; the authority pass reads
# the live-or-recorded context (`launcher_context`). With only the live
# marker consulted, both launcher-projection exemptions were unreachable on
# every CLI path: `meiva cluster health-probe` run from an agent child on a pure
# agent-runner dropped the launcher's runner URLs and fell back to the
# sentinel (env-class red).

from __future__ import annotations

from pathlib import Path

import pytest

from base.host.env import dotenv_boot
from base.tests.config.env.test_dotenv_boot import home_without_db_url

_RECORDED = dotenv_boot.LAUNCHER_PROFILE_ENV_KEY


def _home_without_data_plane_urls(tmp_path: Path) -> Path:
    # meiva:note
    # A home whose `.env` declares neither data-plane URL — the pure agent-runner
    # shape for MEIVA_DB_URL and MEIVA_REDIS_URL together (#4334).
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text("MEIVA_AGENT_HOST_HEALTH_PORT=18035\n")
    return home


def test_recorded_launcher_profile_drops_undeclared_owner_url(tmp_path: Path) -> None:
    # meiva:note
    # The recorded context keeps the live one's runner-shape gate: an
    # inherited OWNER url on an undeclared unit still drops — recording the
    # profile must not widen the exemption.
    env = {
        _RECORDED: "agent",
        "MEIVA_DB_URL": "postgresql://meiva_main:owner-password@127.0.0.1:5433/meiva",
    }

    dotenv_boot.enforce_cluster_env_authority(env, home_without_db_url(tmp_path))

    assert "MEIVA_DB_URL" not in env


def test_recorded_launcher_profile_keeps_undeclared_runner_url(tmp_path: Path) -> None:
    # meiva:note
    # The recorded context opens the same exemption the live marker does: the CLI entry
    # popped MEIVA_PROCESS_PROFILE, and the runner projection must still survive.
    runner_url = "postgresql://meiva_runner:runner-password@127.0.0.1:5433/meiva"
    env = {_RECORDED: "agent", "MEIVA_DB_URL": runner_url}

    dotenv_boot.enforce_cluster_env_authority(env, home_without_db_url(tmp_path))

    assert env["MEIVA_DB_URL"] == runner_url


def test_recorded_launcher_profile_keeps_a_hosted_redis_url(tmp_path: Path) -> None:
    env = {_RECORDED: "agent", "MEIVA_REDIS_URL": "redis://runner:pw@127.0.0.1:6380/0"}

    dotenv_boot.enforce_cluster_env_authority(env, _home_without_data_plane_urls(tmp_path))

    assert env["MEIVA_REDIS_URL"] == "redis://runner:pw@127.0.0.1:6380/0"


def test_recorded_launcher_profile_drops_malformed_db_url(tmp_path: Path) -> None:
    # meiva:note
    # The parse gate holds for the recorded context too: `urlsplit` itself
    # rejects the value (invalid IPv6), so it drops instead of raising.
    env = {_RECORDED: "agent", "MEIVA_DB_URL": "postgresql://[::1"}

    dotenv_boot.enforce_cluster_env_authority(env, home_without_db_url(tmp_path))

    assert "MEIVA_DB_URL" not in env


@pytest.mark.parametrize("value", ["redis://[::1", "redis://"])
def test_recorded_launcher_profile_drops_unparseable_redis_url(tmp_path: Path, value: str) -> None:
    # meiva:note
    # The redis exemption's parse gate: a `urlsplit` failure (invalid IPv6)
    # and a parse with no host both drop — never raise, never keep a value that
    # cannot dial.
    env = {_RECORDED: "agent", "MEIVA_REDIS_URL": value}

    dotenv_boot.enforce_cluster_env_authority(env, _home_without_data_plane_urls(tmp_path))

    assert "MEIVA_REDIS_URL" not in env
