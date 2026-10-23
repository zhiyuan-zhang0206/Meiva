# meiva:note
# Tests for base/host/env/bootstrap.py: fetch config from the gateway.
#
# Everything the module reads is a value the test passes: the environment is a dict, the
# home a `tmp_path`, the clock a float and the `.env` a mapping. The gateway is a real
# HTTP server on a loopback port (`serve_json`), dialed through the real transport; an
# unreachable gateway is a port nothing listens on.


import json
from pathlib import Path

import httpx
import pytest

from base.host.env import bootstrap
from base.host.env.boot_policy import BootstrapFetch, fetch_backoff, serve_flag
from base.host.net.tests.local_http import closed_port_url, serve_json

_NOW = 1_000_000.0
# meiva:note
# No waiting between connect retries and a short read bound; every other value is the
# meiva:note
# production default.
_QUICK = BootstrapFetch(timeout_s=2.0, attempts=2, backoff_step_s=0.0)
_AUTH_REFUSAL = "no database authority"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


def _inject(env: dict[str, str], home: Path, tuning: BootstrapFetch = _QUICK):
    return bootstrap.inject_config_from_gateway(env, home=home, now=_NOW, tuning=tuning)


def _resolve(env: dict[str, str], home: Path, tuning: BootstrapFetch = _QUICK):
    return bootstrap.resolve_bootstrap_values(env, home=home, now=_NOW, tuning=tuning)


def _write_snapshot(
    home: Path, *, base_url: str, age_s: float, values: dict[str, str], version: int | None = None
) -> Path:
    snap = bootstrap.snapshot_path(home)
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text(
        json.dumps(
            {
                "v": bootstrap.SNAPSHOT_VERSION if version is None else version,
                "base_url": base_url,
                "written_at": _NOW - age_s,
                "values": values,
            }
        ),
        encoding="utf-8",
    )
    return snap


# meiva:note
# ── inject: the fetched values are applied to the environment ────────────────


def test_inject_config_updates_environ(home: Path) -> None:
    with serve_json({"MEIVA_DB_URL": "postgresql://injected/x"}) as gateway:
        env = {"MEIVA_GATEWAY_URL": gateway.url}
        result = _inject(env, home)
    assert env["MEIVA_DB_URL"] == "postgresql://injected/x"
    assert result.db_authority_refusal is not None
    assert _AUTH_REFUSAL in result.db_authority_refusal


def test_inject_presents_the_delivered_machine_token(home: Path) -> None:
    with serve_json({}) as gateway:
        env = {"MEIVA_GATEWAY_URL": gateway.url, "MEIVA_API_TOKEN": "machine-token"}
        _inject(env, home)
    assert [r.headers["authorization"] for r in gateway.requests] == ["Bearer machine-token"]
    assert gateway.requests[0].path == "/api/bootstrap"


def test_inject_without_a_token_presents_no_authorization(home: Path) -> None:
    with serve_json({}) as gateway:
        _inject({"MEIVA_GATEWAY_URL": gateway.url}, home)
    assert "authorization" not in gateway.requests[0].headers


def test_inject_derives_missing_gateway_health_url(home: Path) -> None:
    """A pure runner probes the remote gateway, never localhost, when its first
    start carried no explicit health override."""
    with serve_json({}) as gateway:
        env = {"MEIVA_GATEWAY_URL": f"{gateway.url}/"}
        _inject(env, home)
    assert env["MEIVA_GATEWAY_HEALTH_URL"] == f"{gateway.url}/api/health"


def test_inject_preserves_explicit_gateway_health_url(home: Path) -> None:
    with serve_json({}) as gateway:
        env = {
            "MEIVA_GATEWAY_URL": gateway.url,
            "MEIVA_GATEWAY_HEALTH_URL": "http://health-proxy.tailnet/ready",
        }
        _inject(env, home)
    assert env["MEIVA_GATEWAY_HEALTH_URL"] == "http://health-proxy.tailnet/ready"


def test_inject_overwrites_existing_env(home: Path) -> None:
    # Fetched values are authoritative (2026-08-01): a stale value in env/.env —
    # a pre-cutover materialization `_enforce_cluster_env_authority` pushed in,
    # meiva:note
    # or a forwarded copy from a spawning process — is overridden by the gateway's
    # meiva:note
    # view. There is no cache to keep, so nothing "wins" over the fetch.
    with serve_json(
        {"MEIVA_DB_URL": "postgresql://gateway/current", "DEEPSEEK_API_KEY": "fetched-key"}
    ) as gateway:
        env = {
            "MEIVA_GATEWAY_URL": gateway.url,
            "MEIVA_DB_URL": "postgresql://stale/cached",
            "DEEPSEEK_API_KEY": "stale-key",
        }
        _inject(env, home)
    assert env["MEIVA_DB_URL"] == "postgresql://gateway/current"  # fetch wins
    assert env["DEEPSEEK_API_KEY"] == "fetched-key"  # fetch wins


def test_inject_treats_blank_as_absent(home: Path) -> None:
    # defense in depth: the spawn path UNSETS these keys, but inject must also
    # overwrite a stale empty value from the fetch rather than keep it.
    with serve_json({"DEEPSEEK_API_KEY": "real-fetched-key"}) as gateway:
        env = {"MEIVA_GATEWAY_URL": gateway.url, "DEEPSEEK_API_KEY": ""}
        _inject(env, home)
    assert env["DEEPSEEK_API_KEY"] == "real-fetched-key"


def test_inject_without_gateway_url_fails_fast(home: Path) -> None:
    # A pure runner with no MEIVA_GATEWAY_URL (never enrolled) must not start with
    # no config — the error names the remedy.
    with pytest.raises(bootstrap.BootstrapFetchError, match=r"meiva init .*--gateway-url"):
        _inject({}, home)


def test_inject_wraps_fetch_failure(home: Path) -> None:
    # The raw httpx error is wrapped with the operator's remedy; the process
    # must not start with no config.
    url = closed_port_url()
    with pytest.raises(bootstrap.BootstrapFetchError, match=url.removeprefix("http://")):
        _inject({"MEIVA_GATEWAY_URL": url}, home)


# meiva:note
# ── the fetch itself ─────────────────────────────────────────────────────────


def test_fetch_backoff_is_linear_in_the_attempt() -> None:
    assert [fetch_backoff(attempt, 0.5) for attempt in range(3)] == [0.5, 1.0, 1.5]
    assert BootstrapFetch().backoff_step_s == 0.5


def test_fetch_returns_the_validated_payload() -> None:
    with serve_json({"MEIVA_DB_URL": "postgresql://ok/x"}) as gateway:
        out = bootstrap.fetch_bootstrap_config(gateway.url, bearer=None, tuning=_QUICK)
    assert out == {"MEIVA_DB_URL": "postgresql://ok/x"}


def test_fetch_rejects_a_payload_that_is_not_a_flat_string_map() -> None:
    with serve_json({"MEIVA_DB_URL": 5}) as gateway, pytest.raises(TypeError, match="flat"):
        bootstrap.fetch_bootstrap_config(gateway.url, bearer=None, tuning=_QUICK)


def test_fetch_does_not_retry_readtimeout() -> None:
    # a slow response (ReadTimeout) must NOT be retried -- stacking another full
    # timeout would blow past the spawn launch-confirm window.
    slow = BootstrapFetch(timeout_s=0.2, attempts=3, backoff_step_s=0.0)
    with serve_json({}, delay_s=0.6) as gateway, pytest.raises(httpx.ReadTimeout):
        bootstrap.fetch_bootstrap_config(gateway.url, bearer=None, tuning=slow)
    assert len(gateway.requests) == 1  # not retried


def test_fetch_one_attempt_preserves_connect_exception() -> None:
    one = BootstrapFetch(timeout_s=2.0, attempts=1, backoff_step_s=0.0)
    with pytest.raises(httpx.ConnectError):
        bootstrap.fetch_bootstrap_config(closed_port_url(), bearer=None, tuning=one)


def test_fetch_exhausted_connect_retries_raise_the_connect_exception() -> None:
    three = BootstrapFetch(timeout_s=2.0, attempts=3, backoff_step_s=0.0)
    with pytest.raises(httpx.ConnectError):
        bootstrap.fetch_bootstrap_config(closed_port_url(), bearer=None, tuning=three)


# ── config source derivation (MEIVA_CONFIG_SOURCE deleted) ──


def test_config_source_is_local_when_serve_gateway_env_true() -> None:
    assert bootstrap.config_source_is_local({"MEIVA_MACHINE_SERVE_GATEWAY": "true"}, env_file={})


def test_config_source_is_remote_for_pure_runner() -> None:
    assert not bootstrap.config_source_is_local(
        {"MEIVA_MACHINE_SERVE_GATEWAY": "false"}, env_file={}
    )
    assert not bootstrap.config_source_is_local({}, env_file={})  # absent = pure runner


def test_config_source_reads_the_env_file_when_env_is_unset() -> None:
    # meiva:note
    # env unset -> the home's `.env` decides.
    assert bootstrap.config_source_is_local({}, env_file={"MEIVA_MACHINE_SERVE_GATEWAY": "true"})
    assert not bootstrap.config_source_is_local(
        {}, env_file={"MEIVA_MACHINE_SERVE_GATEWAY": "false"}
    )
    assert not bootstrap.config_source_is_local({}, env_file={"machine_serve_gateway": "true"})


def test_a_blank_env_value_defers_to_the_env_file() -> None:
    assert bootstrap.config_source_is_local(
        {"MEIVA_MACHINE_SERVE_GATEWAY": "  "}, env_file={"MEIVA_MACHINE_SERVE_GATEWAY": "yes"}
    )


def test_config_source_env_wins_over_the_env_file() -> None:
    assert not bootstrap.config_source_is_local(
        {"MEIVA_MACHINE_SERVE_GATEWAY": "false"}, env_file={"MEIVA_MACHINE_SERVE_GATEWAY": "true"}
    )


@pytest.mark.parametrize("raw", ["1", "true", "TRUE", " yes ", "on"])
def test_serve_flag_truthy_spellings(raw: str) -> None:
    assert serve_flag("MEIVA_MACHINE_SERVE_GATEWAY", {"MEIVA_MACHINE_SERVE_GATEWAY": raw}, {})


@pytest.mark.parametrize("raw", ["0", "false", "no", "off", "maybe"])
def test_serve_flag_other_spellings_resolve_false(raw: str) -> None:
    # meiva:note
    # A malformed value resolves False here; the real resolver raises on it loudly later.
    assert not serve_flag("MEIVA_MACHINE_SERVE_GATEWAY", {"MEIVA_MACHINE_SERVE_GATEWAY": raw}, {})


def test_config_source_fetch_skip_does_not_change_derivation() -> None:
    # The lite opt-out is orthogonal: derivation still says pure runner.
    env = {
        "MEIVA_MACHINE_SERVE_GATEWAY": "false",
        bootstrap.CONFIG_FETCH_ENV: bootstrap.CONFIG_FETCH_SKIP,
    }
    assert not bootstrap.config_source_is_local(env, env_file={})


def test_should_fetch_only_for_enrolled_runner() -> None:
    """The fetch fires only for a CONFIGURED pure agent-runner: serve_agent_runner
    on AND a gateway URL present. The full decision at Settings build is
    `not config_source_is_local() and should_fetch_from_gateway()`; a gateway
    unit short-circuits at the first half. Bare checkouts and unenrolled runners
    resolve locally."""
    enrolled = {"MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true", "MEIVA_GATEWAY_URL": "http://gw:8000"}
    assert bootstrap.should_fetch_from_gateway(enrolled, env_file={})

    # gateway unit: the caller never reaches should_fetch (local source wins)
    gateway_unit = {**enrolled, "MEIVA_MACHINE_SERVE_GATEWAY": "true"}
    assert bootstrap.config_source_is_local(gateway_unit, env_file={})

    unenrolled = {"MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true"}
    assert not bootstrap.should_fetch_from_gateway(unenrolled, env_file={})

    bare_checkout = {"MEIVA_GATEWAY_URL": "http://gw:8000"}
    assert not bootstrap.should_fetch_from_gateway(bare_checkout, env_file={})


# meiva:note
# The serve_agent_runner flag resolves env > the home's `.env`, settings-free.
def test_should_fetch_reads_the_serve_flag_from_the_env_file() -> None:
    env = {"MEIVA_GATEWAY_URL": "http://gw:8000"}
    assert not bootstrap.should_fetch_from_gateway(env, env_file={})
    assert bootstrap.should_fetch_from_gateway(
        env, env_file={"MEIVA_MACHINE_SERVE_AGENT_RUNNER": "true"}
    )


def test_launcher_context_is_the_live_profile_or_the_recorded_one() -> None:
    assert bootstrap.launcher_context({}) is None
    assert bootstrap.launcher_context({"MEIVA_PROCESS_PROFILE": "agent"}) == "agent"
    recorded = {bootstrap.LAUNCHER_PROFILE_ENV_KEY: "runner"}
    assert bootstrap.launcher_context(recorded) == "runner"
    live_first = {"MEIVA_PROCESS_PROFILE": "agent", **recorded}
    assert bootstrap.launcher_context(live_first) == "agent"
    assert bootstrap.launcher_context({"MEIVA_PROCESS_PROFILE": ""}) is None


# ── P0 #2100: the parent config snapshot ─────────────────────────────────────


def test_inject_skips_fetch_on_fresh_snapshot(home: Path) -> None:
    """P0 #2100: a fresh parent snapshot is authoritative — no HTTP fetch at all."""
    with serve_json({"MEIVA_DB_URL": "postgresql://fetched/x"}) as gateway:
        _write_snapshot(
            home,
            base_url=gateway.url,
            age_s=1.0,
            values={"MEIVA_DB_URL": "postgresql://snapshot/x", "DEEPSEEK_API_KEY": "snap-key"},
        )
        env = {"MEIVA_GATEWAY_URL": gateway.url}
        _inject(env, home)
    assert gateway.requests == []
    assert env["MEIVA_DB_URL"] == "postgresql://snapshot/x"
    assert env["DEEPSEEK_API_KEY"] == "snap-key"
    assert env["MEIVA_GATEWAY_HEALTH_URL"] == f"{gateway.url}/api/health"


def test_the_snapshot_freshness_window_is_a_tuning_value(home: Path) -> None:
    with serve_json({"MEIVA_DB_URL": "postgresql://fetched/x"}) as gateway:
        _write_snapshot(
            home,
            base_url=gateway.url,
            age_s=100.0,
            values={"MEIVA_DB_URL": "postgresql://snapshot/x"},
        )
        env = {"MEIVA_GATEWAY_URL": gateway.url}
        _inject(env, home, BootstrapFetch(snapshot_fresh_s=50.0, backoff_step_s=0.0))
    assert len(gateway.requests) == 1
    assert env["MEIVA_DB_URL"] == "postgresql://fetched/x"


def test_inject_fetches_and_refreshes_stale_snapshot(home: Path) -> None:
    """A stale snapshot fetches as before; the fetch wins AND refreshes the cache."""
    with serve_json({"MEIVA_DB_URL": "postgresql://fresh/x"}) as gateway:
        _write_snapshot(
            home,
            base_url=gateway.url,
            age_s=10_000.0,
            values={"MEIVA_DB_URL": "postgresql://old/x"},
        )
        env = {"MEIVA_GATEWAY_URL": gateway.url}
        _inject(env, home)
        snapshot = bootstrap.read_config_snapshot(gateway.url, home=home, now=_NOW + 3.0)
    assert env["MEIVA_DB_URL"] == "postgresql://fresh/x"  # fetch is authoritative
    assert snapshot == (
        {"MEIVA_DB_URL": "postgresql://fresh/x"},
        3.0,
    )  # refreshed for the next process
    assert bootstrap.snapshot_path(home).stat().st_mode & 0o077 == 0


def test_inject_falls_back_to_stale_snapshot_on_transport_failure(home: Path) -> None:
    """Gateway unreachable + a (stale) snapshot → continue on last-known config.

    The outage scenario behind P0 #2100: no cluster edit can land while the
    gateway is down, so the snapshot is the safest config there is."""
    url = closed_port_url()
    _write_snapshot(
        home, base_url=url, age_s=10_000.0, values={"MEIVA_DB_URL": "postgresql://last-known/x"}
    )
    env = {"MEIVA_GATEWAY_URL": url}
    _inject(env, home)  # must not raise
    assert env["MEIVA_DB_URL"] == "postgresql://last-known/x"


def test_inject_still_raises_on_transport_failure_without_snapshot(home: Path) -> None:
    """No snapshot to fall back on: the honest loud failure is unchanged."""
    with pytest.raises(bootstrap.BootstrapFetchError, match="no snapshot"):
        _inject({"MEIVA_GATEWAY_URL": closed_port_url()}, home)


def test_inject_does_not_fall_back_on_auth_failure(home: Path) -> None:
    """401 (wrong/rotated secret) must fail loud — the snapshot fallback is for
    transport failures only, never for a gateway that rejects this runner."""
    with serve_json({"detail": "unauthorized"}, status=401) as gateway:
        _write_snapshot(
            home,
            base_url=gateway.url,
            age_s=10_000.0,
            values={"MEIVA_DB_URL": "postgresql://old/x"},
        )
        env = {"MEIVA_GATEWAY_URL": gateway.url}
        with pytest.raises(bootstrap.BootstrapFetchError):
            _inject(env, home)
    assert "MEIVA_DB_URL" not in env  # nothing applied


def test_inject_ignores_snapshot_from_another_gateway(home: Path) -> None:
    """A re-enrolled runner must never reuse the previous gateway's config."""
    with serve_json({"MEIVA_DB_URL": "postgresql://new-gw/x"}) as gateway:
        _write_snapshot(
            home,
            base_url="http://old-gateway:9000",
            age_s=1.0,
            values={"MEIVA_DB_URL": "postgresql://old-gw/x"},
        )
        env = {"MEIVA_GATEWAY_URL": gateway.url}
        _inject(env, home)
    assert env["MEIVA_DB_URL"] == "postgresql://new-gw/x"


def test_inject_ignores_a_snapshot_of_another_version(home: Path) -> None:
    with serve_json({"MEIVA_DB_URL": "postgresql://fetched/x"}) as gateway:
        _write_snapshot(
            home,
            base_url=gateway.url,
            age_s=1.0,
            values={"MEIVA_DB_URL": "postgresql://snapshot/x"},
            version=bootstrap.SNAPSHOT_VERSION - 1,
        )
        env = {"MEIVA_GATEWAY_URL": gateway.url}
        _inject(env, home)
    assert env["MEIVA_DB_URL"] == "postgresql://fetched/x"


def test_inject_ignores_malformed_snapshot(home: Path) -> None:
    """Garbage on disk degrades to the fetch path, never kills the boot."""
    snap = bootstrap.snapshot_path(home)
    snap.parent.mkdir(parents=True, exist_ok=True)
    snap.write_text("{not json", encoding="utf-8")
    with serve_json({"MEIVA_DB_URL": "postgresql://fetched/x"}) as gateway:
        env = {"MEIVA_GATEWAY_URL": gateway.url}
        _inject(env, home)
    assert env["MEIVA_DB_URL"] == "postgresql://fetched/x"


# ── resolve_bootstrap_values — the read-only boot resolution (task #4080) ────


# meiva:note
# The watchdog's pre-attempt gate asks the same question `inject_` answers;
# it must get the values without applying them to the environment.
def test_resolve_returns_values_without_mutating_the_environment(home: Path) -> None:
    values = {"MEIVA_DB_URL": "postgresql://fetched/x", "DEEPSEEK_API_KEY": "fetched-key"}
    with serve_json(values) as gateway:
        env = {"MEIVA_GATEWAY_URL": gateway.url}
        assert _resolve(env, home) == values
    assert env == {"MEIVA_GATEWAY_URL": gateway.url}


def test_resolve_uses_a_fresh_snapshot_without_fetching(home: Path) -> None:
    with serve_json({}) as gateway:
        _write_snapshot(
            home,
            base_url=gateway.url,
            age_s=1.0,
            values={"MEIVA_DB_URL": "postgresql://snapshot/x"},
        )
        resolved = _resolve({"MEIVA_GATEWAY_URL": gateway.url}, home)
    assert resolved == {"MEIVA_DB_URL": "postgresql://snapshot/x"}
    assert gateway.requests == []


def test_resolve_falls_back_to_the_stale_snapshot_on_transport_failure(home: Path) -> None:
    url = closed_port_url()
    _write_snapshot(
        home, base_url=url, age_s=10_000.0, values={"MEIVA_DB_URL": "postgresql://last-known/x"}
    )
    assert _resolve({"MEIVA_GATEWAY_URL": url}, home) == {
        "MEIVA_DB_URL": "postgresql://last-known/x"
    }


def test_resolve_raises_without_a_snapshot(home: Path) -> None:
    with pytest.raises(bootstrap.BootstrapFetchError, match="no snapshot"):
        _resolve({"MEIVA_GATEWAY_URL": closed_port_url()}, home)


# meiva:note
# ── the launcher's plugin config carriers ────────────────────────────────────


def test_plugin_cluster_config_values_pick_one_plugin_out_of_the_packet() -> None:
    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV

    packet = json.dumps({"fleet": {"cadence": "daily"}})
    env = {PLUGIN_CLUSTER_CONFIG_ENV: packet}
    assert bootstrap.cluster_plugin_config_values(env, "fleet") == {"cadence": "daily"}
    assert bootstrap.cluster_plugin_config_values(env, "other") is None
    assert bootstrap.cluster_plugin_config_values({}, "fleet") is None


@pytest.mark.parametrize("packet", ["[]", json.dumps({"fleet": []})])
def test_plugin_cluster_config_values_refuse_a_non_object(packet: str) -> None:
    from base.host.env.registry import PLUGIN_CLUSTER_CONFIG_ENV

    with pytest.raises(TypeError, match="JSON object"):
        bootstrap.cluster_plugin_config_values({PLUGIN_CLUSTER_CONFIG_ENV: packet}, "fleet")


def test_service_plugin_config_packet_is_the_launchers_env_value() -> None:
    from base.host.env.registry import SERVICE_PLUGIN_CONFIG_ENV

    assert bootstrap.service_plugin_config_packet({SERVICE_PLUGIN_CONFIG_ENV: "{}"}) == "{}"
    assert bootstrap.service_plugin_config_packet({}) is None
