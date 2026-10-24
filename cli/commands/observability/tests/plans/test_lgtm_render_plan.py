# meiva:note
# What the native LGTM config templates become, which URLs Grafana's provisioning points at and
# what a render warns about, decided from values alone.

from __future__ import annotations

from dataclasses import replace

import pytest

from cli.commands.observability.plans.lgtm.render import (
    NativeRenderValues,
    ReadSurface,
    env_divergences,
    listen_read_warnings,
    render_config,
    render_run_script,
    tempo_topology_warning,
)
from cli.commands.observability.plans.lgtm.urls import (
    AlertWebhook,
    alerts_webhook,
    datasource_urls,
    normalize_gateway_base,
    pg_datasource,
)
from cli.commands.observability.url_split import split_url

# meiva:note
# S104-flagged literal reused by the warning cases: a config value under test, not a bind.
WILDCARD = "0.0.0.0"  # noqa: S104


def values(**changes: str | int) -> NativeRenderValues:
    return replace(
        NativeRenderValues(
            meiva_home="/h",
            storage_dir="/h/lgtm/native/data",
            provisioning_path="/h/lgtm/native/config/provisioning",
            repo="/repo",
            tempo_query_url="http://127.0.0.1:3200",
            loki_url="http://127.0.0.1:3100",
            prometheus_url="http://127.0.0.1:9090",
            pg_url="127.0.0.1:5433",
            alerts_webhook_url="http://127.0.0.1:8000/api/alerts",
            telemetry_loki_url="http://127.0.0.1:3100",
            telemetry_prometheus_url="http://127.0.0.1:9090",
            telemetry_grafana_url="http://127.0.0.1:3003",
            listen_host="127.0.0.1",
            grafana_listen_host=WILDCARD,
            loki_port=3100,
            loki_grpc_port=9095,
            grafana_port=3003,
        ),
        **changes,
    )


def test_a_template_fills_its_values_and_leaves_no_placeholder() -> None:
    rendered = render_config(
        "runtime.env",
        "A={{MEIVA_HOME}}\nP={{GRAFANA_PROVISIONING_PATH}}\nU={{LOKI_URL}}\nS={{LGTM_STORAGE_DIR}}\n",
        values(),
    )

    assert rendered == (
        "A=/h\nP=/h/lgtm/native/config/provisioning/dashboards\n"
        "U=http://127.0.0.1:3100\nS=/h/lgtm/native/data\n"
    )


def test_the_webhook_url_is_shell_quoted_so_its_path_stays_literal_data() -> None:
    rendered = render_config(
        "runtime.env",
        "W={{ALERTS_WEBHOOK_URL}}\n",
        values(alerts_webhook_url="https://gateway.test/tenant/$(touch${IFS}ran)/api/alerts"),
    )

    assert rendered == "W='https://gateway.test/tenant/$(touch${IFS}ran)/api/alerts'\n"


def test_prometheus_scrapes_the_host_port_form_of_the_telemetry_read_urls() -> None:
    template = (
        'prom: "{{MEIVA_PROMETHEUS_SCRAPE_TARGET}}"\ntempo: "{{MEIVA_TEMPO_QUERY_URL}}"\n'
        'loki: "{{MEIVA_LOKI_SCRAPE_TARGET}}"\ngrafana: "{{MEIVA_GRAFANA_SCRAPE_TARGET}}"\n'
    )

    rendered = render_config(
        "prometheus.yml",
        template,
        values(
            tempo_query_url="http://tempo.example:3200",
            telemetry_loki_url="https://10.0.0.5:3100/",
            telemetry_prometheus_url="http://10.0.0.5:9090",
        ),
    )

    assert rendered == (
        'prom: "10.0.0.5:9090"\ntempo: "tempo.example:3200"\n'
        'loki: "10.0.0.5:3100"\ngrafana: "127.0.0.1:3003"\n'
    )


def test_only_prometheus_rewrites_the_tempo_query_url_to_host_port() -> None:
    template = "q={{MEIVA_TEMPO_QUERY_URL}}\n"

    assert render_config("runtime.env", template, values()) == "q=http://127.0.0.1:3200\n"
    assert render_config("prometheus.yml", template, values()) == "q=127.0.0.1:3200\n"


def test_loki_listener_tokens_are_brace_free_so_the_yaml_stays_a_plain_scalar() -> None:
    rendered = render_config(
        "loki.yaml",
        "port: __LGTM_LOKI_PORT__\ngrpc: __LGTM_LOKI_GRPC_PORT__\naddr: __LGTM_LISTEN_HOST__\n",
        values(loki_port=53100, loki_grpc_port=59095, listen_host="10.0.0.5"),
    )

    assert rendered == "port: 53100\ngrpc: 59095\naddr: 10.0.0.5\n"


def test_the_grafana_launcher_gets_the_same_values_as_the_configs() -> None:
    assert render_run_script("home={{MEIVA_HOME}} port={{LGTM_GRAFANA_PORT}}\n", values()) == (
        "home=/h port=3003\n"
    )


def test_a_template_that_names_a_value_nobody_supplies_keeps_the_placeholder() -> None:
    assert render_config("runtime.env", "X={{NOT_A_KEY}}\n", values()) == "X={{NOT_A_KEY}}\n"


def test_no_observatory_keeps_the_configured_read_urls_without_trailing_slashes() -> None:
    assert datasource_urls(
        observatory_base="", loki_url="http://127.0.0.1:3100/", prometheus_url="http://h:9090"
    ) == ("http://127.0.0.1:3100", "http://h:9090")


def test_a_remote_observatory_is_reached_on_each_services_own_port() -> None:
    assert datasource_urls(
        observatory_base="http://10.0.0.46",
        loki_url="http://127.0.0.1:3100",
        prometheus_url="http://127.0.0.1:9090",
    ) == ("http://10.0.0.46:3100", "http://10.0.0.46:9090")


@pytest.mark.parametrize(
    ("host", "port", "expected"),
    [
        ("127.0.0.1", "5433", "127.0.0.1:5433"),
        ("db.test", "25432", "db.test:25432"),
        ("::1", "20027", "[::1]:20027"),
        ("/run/meiva-test", "20027", "127.0.0.1:20027"),
        ("db.test", None, "db.test:5432"),
        (None, None, "127.0.0.1:5432"),
    ],
)
def test_the_pg_datasource_is_the_database_authority_in_tcp_form(
    host: str | None, port: str | None, expected: str
) -> None:
    datasource = pg_datasource(host, port, remote_observatory=False)

    assert datasource.host_port == expected
    assert datasource.warning is None


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1", "/run/meiva-pg"])
def test_a_loopback_database_under_a_remote_observatory_warns(host: str) -> None:
    datasource = pg_datasource(host, "5433", remote_observatory=True)

    assert datasource.warning is not None
    assert "data-plane db_url" in datasource.warning
    assert "will dial the Grafana host's own loopback" in datasource.warning


def test_a_remote_database_under_a_remote_observatory_does_not_warn() -> None:
    assert pg_datasource("10.0.0.72", "5433", remote_observatory=True).warning is None


def webhook(*, gateway_url: str, port: int, remote: bool, host: str = "10.0.0.10") -> AlertWebhook:
    base = normalize_gateway_base(gateway_url)
    return alerts_webhook(
        remote_observatory=remote,
        gateway_base=base,
        gateway=split_url(base),
        gateway_port=port,
        machine_host=host,
    )


@pytest.mark.parametrize(
    ("remote", "gateway_url", "port", "expected"),
    [
        (False, "", 8000, "http://127.0.0.1:8000/api/alerts"),
        (False, "http://gateway.test:20016", 20016, "http://127.0.0.1:20016/api/alerts"),
        (True, "http://gateway.test:20016", 8000, "http://gateway.test:20016/api/alerts"),
        (True, "https://gateway.test/meiva/", 8000, "https://gateway.test/meiva/api/alerts"),
        (True, "http://[::1]:20016", 8000, "http://[::1]:20016/api/alerts"),
        (True, "", 20016, "http://10.0.0.10:20016/api/alerts"),
    ],
)
def test_the_webhook_is_loopback_locally_and_the_reachable_gateway_for_a_remote_observatory(
    remote: bool, gateway_url: str, port: int, expected: str
) -> None:
    assert webhook(gateway_url=gateway_url, port=port, remote=remote).url == expected


def test_only_a_remote_observatory_webhook_carries_the_hairpin_notice() -> None:
    assert webhook(gateway_url="", port=8000, remote=False).notice is None
    notice = webhook(gateway_url="http://gateway.test:20016", port=8000, remote=True).notice
    assert notice is not None
    assert "alert webhook will point at the gateway's reachable address" in notice


def test_a_machine_host_in_ipv6_form_is_bracketed_in_the_fallback_webhook() -> None:
    assert (
        webhook(gateway_url="", port=8000, remote=True, host="fd7a:115c::10").url
        == "http://[fd7a:115c::10]:8000/api/alerts"
    )


@pytest.mark.parametrize(
    "gateway_url",
    [
        "https://user:synthetic-secret@gateway.test",
        "https://gateway.test/?token=synthetic-secret",
        "https://gateway.test/#synthetic-secret",
        "ftp://gateway.test",
        "gateway.test:8000",
    ],
)
def test_a_gateway_base_with_credentials_a_query_a_fragment_or_a_foreign_scheme_is_refused(
    gateway_url: str,
) -> None:
    with pytest.raises(ValueError, match="credential-free") as error:
        webhook(gateway_url=gateway_url, port=8000, remote=True)

    assert "synthetic-secret" not in str(error.value)


def test_the_gateway_base_is_trimmed_of_whitespace_and_a_trailing_slash() -> None:
    assert (
        normalize_gateway_base("  https://gateway.test/meiva/ \n") == "https://gateway.test/meiva"
    )
    assert normalize_gateway_base("") == ""


@pytest.mark.parametrize(
    ("query", "intake", "warns"),
    [
        ("http://127.0.0.1:3200", "http://tempo.example:14318", True),
        ("http://tempo.example:3200", "http://localhost:14318", True),
        ("http://127.0.0.1:3200", "http://localhost:14318", False),
        ("http://tempo.example:3200", "http://collector.example:14318", False),
    ],
)
def test_the_tempo_topology_warns_only_when_query_and_intake_disagree_about_loopback(
    query: str, intake: str, warns: bool
) -> None:
    warning = tempo_topology_warning(
        query_url=query, query=split_url(query), intake_endpoint=intake, intake=split_url(intake)
    )

    if warns:
        assert warning is not None
        assert f"MEIVA_TELEMETRY_TEMPO_QUERY_URL resolves to {query}" in warning
        assert f"Tempo intake endpoint is {intake}" in warning
    else:
        assert warning is None


def surface(backend: str, listen: str, read_url: str) -> ReadSurface:
    env_var = f"MEIVA_TELEMETRY_{backend.upper()}_URL"
    return ReadSurface(backend, env_var, listen, read_url, split_url(read_url))


@pytest.mark.parametrize(
    ("listen", "expected_backends"),
    [
        ("10.0.0.5", ["loki"]),
        (WILDCARD, []),
        ("127.0.0.1", []),
        ("::1", []),
        ("private-box", []),
    ],
)
def test_a_listen_host_is_judged_against_its_read_url(
    listen: str, expected_backends: list[str]
) -> None:
    warnings = listen_read_warnings((surface("loki", listen, "http://127.0.0.1:3100"),))

    assert len(warnings) == len(expected_backends)
    for warning in warnings:
        assert "MEIVA_TELEMETRY_LOKI_URL resolves to http://127.0.0.1:3100" in warning
        assert f"loki listens on {listen}" in warning


def test_a_read_url_that_follows_the_widened_listen_host_does_not_warn() -> None:
    assert listen_read_warnings((surface("loki", "10.0.0.5", "http://10.0.0.5:3100"),)) == ()


def test_each_mismatched_backend_gets_its_own_warning() -> None:
    warnings = listen_read_warnings(
        (
            surface("loki", "10.0.0.5", "http://127.0.0.1:3100"),
            surface("prometheus", "10.0.0.5", "http://10.0.0.5:9090"),
            surface("grafana", "10.0.0.6", "http://localhost:3003"),
        )
    )

    assert [w.split(" resolves")[0].removeprefix("lgtm native: ") for w in warnings] == [
        "MEIVA_TELEMETRY_LOKI_URL",
        "MEIVA_TELEMETRY_GRAFANA_URL",
    ]


def test_a_value_that_disagrees_with_the_env_file_is_called_out() -> None:
    warnings = env_divergences(
        {"MEIVA_TELEMETRY_TEMPO_QUERY_URL": "http://127.0.0.1:3200"},
        {"MEIVA_TELEMETRY_TEMPO_QUERY_URL": "http://10.55.0.9:3200"},
        env_file="/h/.env",
    )

    assert warnings == (
        "lgtm native: MEIVA_TELEMETRY_TEMPO_QUERY_URL resolved to http://10.55.0.9:3200 but "
        "/h/.env declares http://127.0.0.1:3200 — an inherited environment value is in effect",
    )


def test_consistent_undeclared_and_slash_only_differences_stay_quiet() -> None:
    assert (
        env_divergences(
            {"A": "http://x:1/", "B": "same"},
            {"A": "http://x:1", "B": "same", "C": "undeclared in the file: skipped"},
            env_file="/h/.env",
        )
        == ()
    )
