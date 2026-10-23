# meiva:note
# Co-located gateway and runner units each load their own .env: the boot pass
# loads `$MEIVA_HOME/.env`, forces this unit's cluster declarations over a polluted
# parent environment and drops the cluster values it does not declare. The home
# resolution itself is covered by base/tests/config/test_home_resolution.py.

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from base.host.env import dotenv_boot

# meiva:note
# Every test here hands the boot functions a plain dict for the environment and a
# meiva:note
# `tmp_path` home: nothing reads or writes the pytest process's own environment.


# meiva:note
# A fresh unit home whose `.env` holds `env_text`.
def _home(tmp_path: Path, env_text: str = "") -> Path:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    (home / ".env").write_text(env_text)
    return home


def home_without_db_url(tmp_path: Path) -> Path:
    # meiva:note
    # A home whose `.env` declares no MEIVA_DB_URL — the pure agent-runner shape (#4334).
    return _home(tmp_path, "MEIVA_AGENT_HOST_HEALTH_PORT=18035\n")


# meiva:note
# ── enforce_cluster_env_authority: this cluster's .env wins over a polluted parent ──


def test_enforce_overrides_leaked_derived_key(tmp_path: Path) -> None:
    # meiva:note
    # A sibling cluster's health port leaked into the environment (a plain load leaves an
    # already-set key untouched) is corrected by this cluster's own .env — the bug
    # where a preview service bound the production health port.
    env = {"MEIVA_AGENT_HOST_HEALTH_PORT": "8102"}
    home = _home(tmp_path, "MEIVA_AGENT_HOST_HEALTH_PORT=18035\n")
    dotenv_boot.enforce_cluster_env_authority(env, home)
    assert env["MEIVA_AGENT_HOST_HEALTH_PORT"] == "18035"


def test_enforce_ignores_non_enforced_keys(tmp_path: Path) -> None:
    # meiva:note
    # Only the derived + identity keys are forced from the file — an
    # unrelated env var in the file never overrides the live environment.
    # MEIVA_LLM_OVERRIDE is deliberately chosen: an e2e-only override read straight
    # from the environment, not a Settings field, so it has no owning sub-model to
    # enforce (a Settings-backed key like MEIVA_LABELER_MODEL would be forced).
    env = {"MEIVA_LLM_OVERRIDE": "env-live"}
    home = _home(tmp_path, "MEIVA_LLM_OVERRIDE=env-file\n")
    dotenv_boot.enforce_cluster_env_authority(env, home)
    assert env["MEIVA_LLM_OVERRIDE"] == "env-live"


def test_enforce_forces_identity_key_declared_in_env_file(tmp_path: Path) -> None:
    # meiva:note
    # A machine-identity key the unit's own .env declares wins verbatim over an
    # inherited value — same authority rule as the DERIVED cluster keys. A unit's
    # machine identity (serve flags, name) is a per-unit fact; the .env is its
    # home.
    env = {"MEIVA_MACHINE_NAME": "leaked-prod-host"}
    home = _home(tmp_path, "MEIVA_MACHINE_NAME=file-host\n")
    dotenv_boot.enforce_cluster_env_authority(env, home)
    assert env["MEIVA_MACHINE_NAME"] == "file-host"


def test_enforce_drops_undeclared_identity_key(tmp_path: Path) -> None:
    # meiva:note
    # An inherited machine-identity key the unit's .env does NOT declare is
    # dropped — the #771 leak: prod's MEIVA_MACHINE_SERVE_GATEWAY=true riding a
    # login shell into an isolated child flipped `config_source_is_local()` to
    # True, skipped the settings-lite placeholders, and the DERIVED drop then
    # failed Settings with Field required. Dropped, the resolver falls through to
    # False.
    env = {
        "MEIVA_MACHINE_SERVE_GATEWAY": "true",
        "MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true",
        "MEIVA_MACHINE_NAME": "leaked-prod-host",
        "MEIVA_MEMORY_REMOTE": "git@github.com:prod/MeivaMemory.git",
    }
    dotenv_boot.enforce_cluster_env_authority(env, _home(tmp_path))
    assert env == {}


def test_enforce_keeps_timezone_supplied_by_env_alone(tmp_path: Path) -> None:
    # meiva:note
    # An inherited MEIVA_TIMEZONE the unit .env does NOT declare survives the
    # authority pass — the gateway-hosted schedule runner receives the cluster
    # timezone from its spawn env, and dropping it left the runner on the
    # America/Los_Angeles field default (2026-08-21: schedule #3 fired at PT
    # midnight instead of Shanghai midnight after the 08-12 ruling). The gateway
    # is the cluster's timezone authority; a pure agent-runner re-injects the
    # authoritative value via /api/bootstrap at Settings build regardless.
    env = {"MEIVA_TIMEZONE": "Asia/Shanghai"}
    dotenv_boot.enforce_cluster_env_authority(env, _home(tmp_path))
    assert env["MEIVA_TIMEZONE"] == "Asia/Shanghai"


def test_enforce_forces_declared_tempo_urls_over_forwarded_snapshot(tmp_path: Path) -> None:
    # meiva:note
    # The host-scope tempo URLs the unit's .env declares win over an inherited
    # copy. Session children receive host-scope facts by env forward, so a parent
    # that booted before the .env change hands its stale pair to every child and
    # the station's converge re-renders with the old URL (2026-09-14 wave, task
    # #3339).
    env = {
        "MEIVA_TELEMETRY_TEMPO_QUERY_URL": "http://10.55.0.9:3200",
        "MEIVA_TELEMETRY_TEMPO_ENDPOINT": "http://10.55.0.9:14318",
    }
    home = _home(
        tmp_path,
        "MEIVA_TELEMETRY_TEMPO_QUERY_URL=http://127.0.0.1:3200\n"
        "MEIVA_TELEMETRY_TEMPO_ENDPOINT=http://127.0.0.1:14318\n",
    )
    dotenv_boot.enforce_cluster_env_authority(env, home)
    assert env["MEIVA_TELEMETRY_TEMPO_QUERY_URL"] == "http://127.0.0.1:3200"
    assert env["MEIVA_TELEMETRY_TEMPO_ENDPOINT"] == "http://127.0.0.1:14318"


def test_enforce_keeps_tempo_urls_supplied_by_env_alone(tmp_path: Path) -> None:
    # meiva:note
    # Undeclared tempo URLs stay untouched — env-only supply remains legitimate
    # (a not-yet-enrolled unit, the test suites).
    env = {"MEIVA_TELEMETRY_TEMPO_QUERY_URL": "http://tempo.test:3200"}
    home = _home(tmp_path, "MEIVA_TIMEZONE=Asia/Shanghai\n")
    dotenv_boot.enforce_cluster_env_authority(env, home)
    assert env["MEIVA_TELEMETRY_TEMPO_QUERY_URL"] == "http://tempo.test:3200"


def test_declared_service_path_wins_over_forwarded_snapshot(tmp_path: Path) -> None:
    env = {"MEIVA_SERVICE_PATH": str(tmp_path / "stale")}
    declared = str(tmp_path / "declared")
    home = _home(tmp_path, f"MEIVA_SERVICE_PATH='{declared}'\n")
    dotenv_boot.enforce_cluster_env_authority(env, home)
    assert env["MEIVA_SERVICE_PATH"] == declared


def test_spawned_child_reads_forwarded_timezone_without_env_key(
    tmp_path: Path,
) -> None:
    # meiva:note
    # The 2026-08-21 incident chain, end to end: a schedule-runner child
    # spawned with MEIVA_TIMEZONE in its env resolves settings.general.timezone to
    # that value even when its unit .env does not declare the key. Before the
    # never-drop exemption the authority pass popped the forwarded value and the
    # America/Los_Angeles field default won — schedule #3 fired at PT midnight
    # instead of Shanghai midnight.
    child_env = dict(os.environ)
    # meiva:note
    # a unit .env with no MEIVA_TIMEZONE
    child_env["MEIVA_HOME"] = str(tmp_path)
    child_env["MEIVA_TIMEZONE"] = "Asia/Shanghai"
    out = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(Path(__file__).with_name("_env_import_child.py")),
            "forwarded_timezone",
        ],
        env=child_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "Asia/Shanghai"


def test_enforce_keeps_gateway_url_supplied_by_env_alone(tmp_path: Path) -> None:
    # meiva:note
    # The host-scoped gateway URL key stays exempt from the identity drop: a
    # not-yet-enrolled runner (and the test suites) supply MEIVA_GATEWAY_URL from
    # the environment alone, and popping it would silently un-configure the
    # bootstrap fetch (should_fetch_from_gateway goes False).
    env = {"MEIVA_GATEWAY_URL": "http://gw:8000"}
    dotenv_boot.enforce_cluster_env_authority(env, _home(tmp_path))
    assert env["MEIVA_GATEWAY_URL"] == "http://gw:8000"


# meiva:note
# ── enforce_cluster_env_authority: undeclared DERIVED keys are dropped ──
#
# A DERIVED key in the unit's own .env is forced over a polluted parent env; a
# key inherited WITHOUT a .env declaration is dropped, not kept: a pure
# agent-runner's .env carries no cluster data-plane keys (MEIVA_DB_URL /
# MEIVA_REDIS_URL / MEIVA_APP_PORT / ... — they come from the gateway's
# /api/bootstrap at Settings build), and an inherited sibling-cluster value
# sourced into the shell would otherwise leak the sibling's secrets into every
# child process. The fallback is the bootstrap-fetched value (runner data-plane
# keys). (MEIVA_PGBOUNCER_PORT itself is retired — the pooler port is a registry
# fact, never an env key.)


def test_enforce_drops_app_port_absent_from_env_file(tmp_path: Path) -> None:
    # meiva:note
    # An inherited MEIVA_APP_PORT with no declaration in this unit's .env is
    # removed, so a sibling cluster's value cannot stand in a pure runner (the
    # pure-runner case: cluster data-plane keys arrive via bootstrap).
    env = {"MEIVA_APP_PORT": "3001"}
    dotenv_boot.enforce_cluster_env_authority(env, _home(tmp_path))
    assert "MEIVA_APP_PORT" not in env


def test_enforce_keeps_app_port_declared_in_env_file(tmp_path: Path) -> None:
    # meiva:note
    # A gateway's own .env declaration wins verbatim over an inherited value —
    # the cluster-isolation authority rule unchanged.
    env = {"MEIVA_APP_PORT": "3001"}
    home = _home(tmp_path, "MEIVA_APP_PORT=18113\n")
    dotenv_boot.enforce_cluster_env_authority(env, home)
    assert env["MEIVA_APP_PORT"] == "18113"


def test_enforce_drops_every_undeclared_derived_key(tmp_path: Path) -> None:
    # meiva:note
    # The absent-key drop is general, not pgbouncer-only: any DERIVED
    # data-plane key this unit's .env does not declare is removed from the
    # environment, so a sibling cluster's value sourced into the shell
    # (MEIVA_APP_PORT / MEIVA_EVENTS_CHANNEL / ...) cannot stand in a pure runner's
    # process. The gateway's bootstrap fetch re-injects the real values at
    # Settings build. (The host-scoped identity keys — MEIVA_GATEWAY_URL, the
    # health ports — are exempt.)
    env = {
        "MEIVA_APP_PORT": "3001",
        "MEIVA_EVENTS_CHANNEL": "meiva:events:leaked",
        "MEIVA_MEMORY_SEARCH_PORT": "19531",
    }
    dotenv_boot.enforce_cluster_env_authority(env, _home(tmp_path))
    assert env == {}


def test_enforce_keeps_placeholder_url(tmp_path: Path) -> None:
    # meiva:note
    # The placeholder DB URL is exempt from the drop: a process tree that
    # carries it (planted by a lite boot) keeps it, and popping it would replace
    # the named refusal with a no-default Settings failure. The home's `.env`
    # declares no database URL, and the placeholder only exists in the environment.
    env = {"MEIVA_DB_URL": dotenv_boot.PLACEHOLDER_DB_URL}
    dotenv_boot.enforce_cluster_env_authority(env, home_without_db_url(tmp_path))
    assert env["MEIVA_DB_URL"] == dotenv_boot.PLACEHOLDER_DB_URL


@pytest.mark.parametrize(
    "url", ["redis://undeclared@127.0.0.1:1/0", "redis://first-start-boot@127.0.0.1:1/0"]
)
def test_enforce_drops_undeclared_redis_url(tmp_path: Path, url: str) -> None:
    # meiva:note
    # Initialization declares real local URLs before constructing Settings.
    env = {"MEIVA_REDIS_URL": url}
    dotenv_boot.enforce_cluster_env_authority(env, home_without_db_url(tmp_path))
    assert "MEIVA_REDIS_URL" not in env


def test_enforce_keeps_placeholder_url_over_a_declaring_env_file(tmp_path: Path) -> None:
    # meiva:note
    # The placeholder outranks an `.env` that DOES declare MEIVA_DB_URL.
    #
    # This is the case that matters and the one the test above cannot reach: the
    # force-assign loop would otherwise overwrite the placeholder a lite boot had
    # planted, and the drop loop's guard never sees it. A process tree that
    # carries the placeholder must not come out of the pass holding a real
    # database URL.
    #
    # Other declared cluster keys must still be forced from the file — the guard is
    # about the placeholder, not about disabling the authority pass.
    env = {
        "MEIVA_DB_URL": dotenv_boot.PLACEHOLDER_DB_URL,
        "MEIVA_EVENTS_CHANNEL": "leaked-from-a-sibling",
    }
    home = _home(
        tmp_path,
        "MEIVA_DB_URL=postgresql://someone_else@127.0.0.1:5433/not_ours\n"
        "MEIVA_EVENTS_CHANNEL=meiva:thisunit:events\n"
        "MEIVA_AGENT_HOST_HEALTH_PORT=18035\n",
    )

    dotenv_boot.enforce_cluster_env_authority(env, home)

    assert env["MEIVA_DB_URL"] == dotenv_boot.PLACEHOLDER_DB_URL
    assert env["MEIVA_EVENTS_CHANNEL"] == "meiva:thisunit:events", (
        "the authority pass must still force every other declared cluster key"
    )


# meiva:note
# ── Per-process profile: gateway pop vs agent/runner survival ──


def test_enforce_drops_a_leaked_redis_url(tmp_path: Path) -> None:
    # meiva:note
    # The exemption is placeholder-EXACT: a sibling unit's real MEIVA_REDIS_URL
    # leaked in via the parent env is still dropped when this unit's .env does
    # not declare one. Neither launcher-context key is present: the #4334 redis
    # exemption needs an agent-launched tree, and this is a plain shell.
    env = {"MEIVA_REDIS_URL": "redis://sibling:6379/0"}
    dotenv_boot.enforce_cluster_env_authority(env, home_without_db_url(tmp_path))
    assert "MEIVA_REDIS_URL" not in env


@pytest.mark.parametrize(
    "profile,key_should_survive",
    [
        # meiva:note
        # profile=gateway → agent-runner cluster keys are popped
        ("gateway", False),
        # meiva:note
        # profile=agent → agent-runner cluster keys survive (agent needs them)
        ("agent", True),
        # meiva:note
        # profile=runner → agent-runner cluster keys survive (daemon needs them)
        ("runner", True),
        # meiva:note
        # no profile → no pop (bare checkout / test / script)
        (None, True),
    ],
)
def test_gateway_profile_pops_only_for_gateway(
    tmp_path: Path,
    profile: str | None,
    key_should_survive: bool,
) -> None:
    # meiva:note
    # Agent-runner cluster-scoped keys are popped from the environment ONLY when
    # MEIVA_PROCESS_PROFILE=gateway. Agent processes (profile=agent), runner daemons
    # (profile=runner), and unprofiled processes (tests, bare checkouts) keep them.
    #
    # This is the regression test for the bug where _is_gateway_process() used
    # MEIVA_MACHINE_SERVE_GATEWAY (a unit capability flag true on every single-box
    # process) instead of MEIVA_PROCESS_PROFILE (an explicit process-type marker).
    # meiva:note
    # Pick a representative agent-runner cluster-scoped key that is in
    # AGENT_RUNNER_CLUSTER_ALIASES. DEEPSEEK_API_KEY is cluster-pinned
    # agent-runner — the gateway must NOT carry it in its environment.
    test_key = "DEEPSEEK_API_KEY"
    env = {test_key: "sk-test-value"}
    if profile is not None:
        env["MEIVA_PROCESS_PROFILE"] = profile

    # meiva:note
    # DEEPSEEK_API_KEY is declared in the `.env` — a single-box unit's .env carries every
    # cluster field (its first start writes them all), so the FORCE branch keeps it in
    # every profile and the ONLY thing that can remove it is the gateway profile pop. (An
    # undeclared cluster key is dropped for every profile — that is the F-s4-4 authority
    # rule, not the profile pop this test guards.)
    home = _home(tmp_path, f"{test_key}=sk-test-value\n")

    dotenv_boot.enforce_cluster_env_authority(env, home)

    if key_should_survive:
        assert env[test_key] == "sk-test-value", (
            f"DEEPSEEK_API_KEY should survive for profile={profile!r} (agent/runner/CLI need it)"
        )
    else:
        assert test_key not in env, (
            "DEEPSEEK_API_KEY should be popped for profile=gateway "
            "(gateway process does not need agent keys)"
        )


# meiva:note
# ── Labeler boot: profile-less spawn keeps the LLM provider keys (task #1230) ──


def test_labeler_boot_without_profile_marker_reads_provider_key_from_env(tmp_path: Path) -> None:
    # meiva:note
    # End to end: the labeler's session env (the gateway session-forward
    # allowlist — cluster-scope provider keys deliberately NOT forwarded, and no
    # MEIVA_PROCESS_PROFILE marker after the services.supervision.roster.spec opt-out, task #1230) boots
    # against a unit .env that declares DEEPSEEK_API_KEY; the key must resolve
    # through the DeepSeek provider plugin's DEEPSEEK_API_KEY environment seam.
    #
    # This is the regression test for the labeler's RuntimeError('DEEPSEEK_API_KEY
    # not set') retry loop: the .env re-source (F-s4-4 force branch) restores the
    # key, and the gateway-profile pop must NOT remove it again — the pop only
    # runs for processes carrying MEIVA_PROCESS_PROFILE=gateway, and the labeler's
    # spec no longer sets it.
    # meiva:note
    # The labeler's session env carries no provider key and no profile marker.
    from base.config import field_alias

    env: dict[str, str] = {}
    home = _home(tmp_path, "DEEPSEEK_API_KEY=sk-labeler-test\n")

    dotenv_boot.load_meiva_env(env, home=home)

    # meiva:note
    # The key lands under the alias the `lm.deepseek_api_key` Settings field reads.
    assert env[field_alias("deepseek_api_key")] == "sk-labeler-test"


def test_gateway_marker_still_drops_provider_keys_for_other_daemons(tmp_path: Path) -> None:
    # meiva:note
    # The mirror image: a daemon that DOES carry MEIVA_PROCESS_PROFILE=gateway
    # (heartbeat, events-maintenance, im-bridge, ...) still loses the provider
    # keys at boot — the labeler's opt-out must not spread the keys into every
    # gateway-profile daemon (anti-spread, task #1230).
    env = {"MEIVA_PROCESS_PROFILE": "gateway"}
    home = _home(tmp_path, "DEEPSEEK_API_KEY=sk-labeler-test\n")

    dotenv_boot.load_meiva_env(env, home=home)

    assert "DEEPSEEK_API_KEY" not in env


def test_agent_profile_keeps_an_unmodeled_provider_key_from_dotenv(tmp_path: Path) -> None:
    # meiva:note
    # The authority pass leaves a plugin key in the agent parent's live env.
    #
    # Provider bindings deliberately do not add Settings fields, so the agent
    # process (and its exec children, which run the same boot) read their declared
    # key from the unit's `.env`.
    env = {"MEIVA_PROCESS_PROFILE": "agent"}
    home = _home(tmp_path, "TESTP_API_KEY=sk-plugin-test\n")

    dotenv_boot.load_meiva_env(env, home=home)

    assert env["TESTP_API_KEY"] == "sk-plugin-test"


def test_agent_profile_keeps_launcher_runner_url_and_drops_admin_passwords(tmp_path: Path) -> None:
    # meiva:note
    # The gateway's .env holds the database endpoint and Redis admin passwords,
    # but an agent must retain the launcher's runner projection and scrub every
    # unprojected data-plane password.
    env = {
        "MEIVA_PROCESS_PROFILE": "agent",
        "MEIVA_REDIS_ADMIN_PASSWORD": "redis-admin-only",
        "MEIVA_REDIS_PASSWORD": "redis-runtime-only",
        "MEIVA_DB_URL": "postgresql://meiva_runner:runner-password@127.0.0.1:5433/meiva",
    }
    home = _home(
        tmp_path,
        "MEIVA_DB_URL=postgresql://meiva@127.0.0.1:5433/meiva\n"
        "MEIVA_REDIS_ADMIN_PASSWORD=redis-admin-only\n"
        "MEIVA_REDIS_PASSWORD=redis-runtime-only\n",
    )

    dotenv_boot.enforce_cluster_env_authority(env, home)

    assert env["MEIVA_DB_URL"].startswith("postgresql://meiva_runner:")
    assert "MEIVA_REDIS_ADMIN_PASSWORD" not in env
    assert "MEIVA_REDIS_PASSWORD" not in env


def test_agent_profile_keeps_undeclared_launcher_runner_url(tmp_path: Path) -> None:
    # meiva:note
    # #4036/#4334: on a unit whose `.env` does NOT declare MEIVA_DB_URL (a pure
    # agent-runner), the launcher-injected runner projection must survive the
    # drop — it is the agent child's only DB source in settings-lite / CLI
    # paths, and popping it left the placeholder URL (probe population
    # check misread as an environment failure).
    runner_url = "postgresql://meiva_runner:runner-password@127.0.0.1:5433/meiva"
    env = {
        "MEIVA_PROCESS_PROFILE": "agent",
        "MEIVA_DB_URL": runner_url,
        # meiva:note
        # Companion (review nit): the exemption must not spill — inherited keys the
        # `.env` does not declare still drop in the SAME pass.
        "MEIVA_APP_PORT": "3001",
        "MEIVA_REDIS_ADMIN_PASSWORD": "redis-admin-only",
    }

    dotenv_boot.enforce_cluster_env_authority(env, home_without_db_url(tmp_path))

    assert env["MEIVA_DB_URL"] == runner_url
    assert "MEIVA_APP_PORT" not in env
    assert "MEIVA_REDIS_ADMIN_PASSWORD" not in env


def test_agent_profile_drops_undeclared_owner_url(tmp_path: Path) -> None:
    # meiva:note
    # The keep is runner-shaped: an inherited OWNER url on an undeclared unit
    # is still dropped in an agent profile (sibling-leak protection unchanged).
    env = {
        "MEIVA_PROCESS_PROFILE": "agent",
        "MEIVA_DB_URL": "postgresql://meiva_main:owner-password@127.0.0.1:5433/meiva",
    }

    dotenv_boot.enforce_cluster_env_authority(env, home_without_db_url(tmp_path))

    assert "MEIVA_DB_URL" not in env


def test_plain_process_drops_undeclared_runner_url(tmp_path: Path) -> None:
    # meiva:note
    # The keep is agent-profile-only: without a launcher context (live or
    # recorded) an inherited runner url is dropped like any other undeclared
    # cluster value.
    env = {"MEIVA_DB_URL": "postgresql://meiva_runner:runner-password@127.0.0.1:5433/meiva"}

    dotenv_boot.enforce_cluster_env_authority(env, home_without_db_url(tmp_path))

    assert "MEIVA_DB_URL" not in env


def test_agent_profile_drops_malformed_undeclared_db_url(tmp_path: Path) -> None:
    # meiva:note
    # Review nit (#3111): a malformed inherited MEIVA_DB_URL — `urlsplit` itself
    # rejects it (invalid IPv6) — is not a projection; the drop loop absorbs it
    # like any other unrecognized value. Before the hardening it raised out of
    # `load_meiva_env` (`ValueError: Invalid IPv6 URL`; e2e repro on the PR).
    env = {"MEIVA_PROCESS_PROFILE": "agent", "MEIVA_DB_URL": "postgresql://[::1"}

    dotenv_boot.enforce_cluster_env_authority(env, home_without_db_url(tmp_path))

    assert "MEIVA_DB_URL" not in env


# meiva:note
# ── load_meiva_env: the files are read into a given environment ──────────────


def test_load_pins_the_home_and_loads_the_env_file(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    home = _home(tmp_path, "MEIVA_LLM_OVERRIDE=from-file\n")

    dotenv_boot.load_meiva_env(env, home=home)

    assert env["MEIVA_HOME"] == str(home)
    assert env["MEIVA_LLM_OVERRIDE"] == "from-file"


def test_load_keeps_an_already_pinned_home_and_never_overrides_a_set_key(tmp_path: Path) -> None:
    env = {"MEIVA_HOME": "/elsewhere", "MEIVA_LLM_OVERRIDE": "live"}
    home = _home(tmp_path, "MEIVA_LLM_OVERRIDE=from-file\n")

    dotenv_boot.load_meiva_env(env, home=home)

    assert env["MEIVA_HOME"] == "/elsewhere"
    assert env["MEIVA_LLM_OVERRIDE"] == "live"


def test_load_reads_mirror_env_after_the_env_file(tmp_path: Path) -> None:
    # meiva:note
    # Precedence: real environment > .env > mirror.env.
    home = _home(tmp_path, "FIRST_KEY=from-env-file\n")
    (home / "mirror.env").write_text("FIRST_KEY=from-mirror\nSECOND_KEY=from-mirror\n")
    env: dict[str, str] = {}

    dotenv_boot.load_meiva_env(env, home=home)

    assert env["FIRST_KEY"] == "from-env-file"
    assert env["SECOND_KEY"] == "from-mirror"


def test_load_interpolates_against_the_given_environment(tmp_path: Path) -> None:
    # meiva:note
    # `${VAR}` in a file resolves against the environment handed in (never the process's),
    # meiva:note
    # a variable defined earlier in the same file, and an inline default.
    home = _home(
        tmp_path,
        "BASE_DIR=${ROOT}/base\nDERIVED=${BASE_DIR}/derived\nFALLBACK=${MISSING:-dflt}\n",
    )
    env = {"ROOT": "/srv"}

    dotenv_boot.load_meiva_env(env, home=home)

    assert env["BASE_DIR"] == "/srv/base"
    assert env["DERIVED"] == "/srv/base/derived"
    assert env["FALLBACK"] == "dflt"


def test_load_keeps_one_python_index_across_its_two_aliases(tmp_path: Path) -> None:
    # meiva:note
    # An inherited legacy alias is normalized before a lower-priority layer can supply the other.
    home = _home(tmp_path, "UV_DEFAULT_INDEX=https://file.example/simple\n")
    inherited = {"UV_INDEX_URL": "https://inherited.example/simple"}

    dotenv_boot.load_meiva_env(inherited, home=home)

    assert inherited["UV_DEFAULT_INDEX"] == "https://inherited.example/simple"
    fresh: dict[str, str] = {}
    dotenv_boot.load_meiva_env(fresh, home=home)
    assert fresh["UV_DEFAULT_INDEX"] == "https://file.example/simple"


def test_load_clears_blank_harness_keys_before_loading(tmp_path: Path) -> None:
    env = {"MEIVA_HARNESS_HANDLE": "h1", "MEIVA_LLM_OVERRIDE": ""}
    home = _home(tmp_path, "MEIVA_LLM_OVERRIDE=from-file\n")

    dotenv_boot.load_meiva_env(env, home=home)

    assert env["MEIVA_LLM_OVERRIDE"] == "from-file"


def test_load_without_any_file_only_pins_the_home(tmp_path: Path) -> None:
    home = tmp_path / "bare"
    home.mkdir()
    env: dict[str, str] = {}

    dotenv_boot.load_meiva_env(env, home=home)

    assert env == {"MEIVA_HOME": str(home)}


# meiva:note
# The "CLI launcher context (#4334)" section moved to
# test_dotenv_boot_launcher_context.py (structure-lint split).
