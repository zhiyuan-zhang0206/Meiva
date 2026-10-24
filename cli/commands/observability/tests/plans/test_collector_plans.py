# meiva:note
# What the collector sidecar's config contains and whether a converge pass installs it, decided
# from values alone: the data-plane receivers, the authenticated remote ingress, the exporter
# fan-out and the install step.

from __future__ import annotations

import pytest
import yaml

from cli.commands.observability.plans.collector.data_plane import (
    PEER_PLACEHOLDER,
    DataPlanePlan,
    data_plane_fragments,
    plan_data_plane_receivers,
    postgres_receiver_block,
    receiver_endpoint,
    redis_receiver_block,
)
from cli.commands.observability.plans.collector.exporters import (
    ExporterShape,
    exporter_shape,
    fanout_bases,
    render_exporters,
)
from cli.commands.observability.plans.collector.install import (
    InstallCollector,
    KeepCollector,
    SkipUnsupportedPlatform,
    plan_collector_install,
    plan_collector_step,
)
from cli.commands.observability.plans.collector.remote import (
    NO_REMOTE_INGRESS,
    RemoteFragments,
    cluster_bearer,
    gateway_ingress_endpoint,
    ingress_endpoint_problem,
    remote_receiver_fragments,
)
from cli.commands.observability.plans.collector.render import (
    CollectorValues,
    render_collector_config,
)
from cli.commands.observability.plans.collector.text import ottl_quote, yaml_quote
from cli.commands.observability.url_split import split_url

# meiva:note
# The telemetry token of the unit under test: a fixture value, not a credential.
TOKEN = "tok"  # noqa: S105

GATEWAY = frozenset({"gateway"})
RUNNER = frozenset({"agent-runner"})
STATION = frozenset({"observability-station"})


def plan(
    roles: frozenset[str] | None,
    *,
    placeholder: bool = False,
    otlp: bool = True,
    remote_plane: bool = False,
) -> DataPlanePlan:
    return plan_data_plane_receivers(
        roles=roles, placeholder_db_url=placeholder, otlp_enabled=otlp, remote_plane=remote_plane
    )


def test_a_gateway_unit_scrapes_its_own_postgres_and_redis() -> None:
    assert plan(GATEWAY | RUNNER) == DataPlanePlan(postgres=True, redis=True)


@pytest.mark.parametrize(
    "roles", [None, RUNNER, STATION], ids=["unconfigured", "runner", "station"]
)
def test_a_unit_without_the_gateway_capability_scrapes_no_data_plane(
    roles: frozenset[str] | None,
) -> None:
    assert plan(roles) == DataPlanePlan(postgres=False, redis=False)


def test_a_never_dialed_placeholder_database_url_means_no_plane_exists_yet() -> None:
    assert plan(GATEWAY, placeholder=True) == DataPlanePlan(postgres=False, redis=False)


def test_disabling_otlp_export_keeps_redis_but_not_the_postgres_receiver() -> None:
    assert plan(GATEWAY, otlp=False) == DataPlanePlan(postgres=False, redis=True)


def test_a_remote_managed_plane_is_scraped_for_redis_only() -> None:
    assert plan(GATEWAY, remote_plane=True) == DataPlanePlan(postgres=False, redis=True)


def test_the_postgres_block_dials_the_homes_socket_as_the_peer_authenticated_role() -> None:
    block = yaml.safe_load(
        "receivers:"
        + postgres_receiver_block(
            socket_dir="/h/.meiva/pg-socket", port=5433, user="meiva_monitor", database="meiva"
        )
    )["receivers"]["postgresql"]

    assert block["endpoint"] == "h/.meiva/pg-socket:5433"
    assert block["transport"] == "unix"
    assert block["username"] == "meiva_monitor"
    assert block["password"] == PEER_PLACEHOLDER
    assert block["databases"] == ["meiva"]


# meiva:note
# The password is a fixture value, never a credential.
def test_the_redis_block_quotes_its_endpoint_and_password() -> None:
    block = yaml.safe_load(
        "receivers:" + redis_receiver_block(endpoint="127.0.0.1:6380", password="it's: a secret")  # noqa: S106
    )["receivers"]["redis"]

    assert block["endpoint"] == "127.0.0.1:6380"
    assert block["password"] == "it's: a secret"  # noqa: S105


def test_the_fragments_list_blocks_and_pipeline_receivers_in_order() -> None:
    both = data_plane_fragments(
        DataPlanePlan(postgres=True, redis=True), postgres_block="[PG]", redis_block="[REDIS]"
    )
    redis_only = data_plane_fragments(
        DataPlanePlan(postgres=False, redis=True), postgres_block="[PG]", redis_block="[REDIS]"
    )
    nothing = data_plane_fragments(
        DataPlanePlan(postgres=False, redis=False), postgres_block="[PG]", redis_block="[REDIS]"
    )

    assert both == ("[PG][REDIS]", ", postgresql, redis")
    assert redis_only == ("[REDIS]", ", redis")
    assert nothing == ("", "")


def test_a_receiver_endpoint_is_the_urls_host_and_port() -> None:
    assert (
        receiver_endpoint(split_url("redis://meiva:pw@10.0.0.2:6380/0"), "redis") == "10.0.0.2:6380"
    )


def test_a_connection_url_without_a_port_cannot_be_scraped() -> None:
    with pytest.raises(RuntimeError, match="redis URL has no host:port"):
        receiver_endpoint(split_url("redis://10.0.0.2/0"), "redis")


@pytest.mark.parametrize(
    "endpoint",
    ["http://example.test:54318", "http://10.0.0.5:4318", "http://10.0.0.5:4318/"],
)
def test_a_non_loopback_http_ingress_with_a_port_is_valid(endpoint: str) -> None:
    assert ingress_endpoint_problem(split_url(endpoint)) is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "",
        "http://localhost:54318",
        "http://127.0.0.1:4318",
        "http://0.0.0.0:54318",
        "http://[::]:54318",
        "https://example.test:54318",
        "http://example.test",
        "http://user:pass@example.test:54318",
        "http://example.test:54318/other",
        "http://example.test:54318?x=y",
        "http://example.test:54318#fragment",
    ],
)
def test_a_loopback_wildcard_secure_portless_or_decorated_ingress_is_invalid(endpoint: str) -> None:
    problem = ingress_endpoint_problem(split_url(endpoint))

    assert problem is not None
    assert "non-loopback" in problem


def test_a_valid_gateway_ingress_loses_its_trailing_slash() -> None:
    raw = "http://10.0.0.5:4318/"

    assert gateway_ingress_endpoint(raw, split_url(raw)) == "http://10.0.0.5:4318"


def test_an_invalid_gateway_ingress_fails_closed_until_the_gateway_publishes_one() -> None:
    with pytest.raises(RuntimeError, match="gateway bootstrap"):
        gateway_ingress_endpoint("http://127.0.0.1:4318", split_url("http://127.0.0.1:4318"))


def test_the_bearer_header_carries_the_token() -> None:
    assert cluster_bearer("tok") == "Bearer tok"


@pytest.mark.parametrize("token", [None, ""])
def test_a_relay_with_no_token_fails_closed(token: str | None) -> None:
    with pytest.raises(RuntimeError, match="telemetry token"):
        cluster_bearer(token)


def remote(
    roles: frozenset[str] | None, host: str = "10.0.0.10", token: str | None = TOKEN
) -> RemoteFragments:
    return remote_receiver_fragments(roles, host=host, token=token, ingress_port=4318)


@pytest.mark.parametrize("roles", [GATEWAY, STATION, GATEWAY | RUNNER])
def test_a_gateway_or_station_with_a_token_and_a_routable_host_serves_remote_peers(
    roles: frozenset[str],
) -> None:
    fragments = remote(roles)
    receiver = yaml.safe_load("receivers:" + fragments.receiver)["receivers"]["otlp/remote"]

    assert receiver == {
        "protocols": {
            "http": {
                "endpoint": "10.0.0.10:4318",
                "auth": {"authenticator": "bearertokenauth/cluster"},
            }
        }
    }
    assert yaml.safe_load("extensions:" + fragments.auth_extension) == {
        "extensions": {"bearertokenauth/cluster": {"token": "tok"}}
    }
    assert fragments.auth_service_extension == ", bearertokenauth/cluster"
    assert fragments.pipeline_receiver == ", otlp/remote"
    traces = yaml.safe_load("pipelines:" + fragments.trace_pipeline)["pipelines"]["traces/remote"]
    assert traces["exporters"] == ["otlphttp/tempo"]


def test_the_remote_receiver_binds_an_ipv6_host_in_brackets() -> None:
    receiver = yaml.safe_load("r:" + remote(GATEWAY, host="fd7a:115c::10").receiver)["r"]

    assert receiver["otlp/remote"]["protocols"]["http"]["endpoint"] == "[fd7a:115c::10]:4318"


@pytest.mark.parametrize("roles", [None, RUNNER])
def test_an_unconfigured_unit_and_a_pure_runner_serve_no_remote_peers(
    roles: frozenset[str] | None,
) -> None:
    assert remote(roles) == NO_REMOTE_INGRESS


@pytest.mark.parametrize(
    ("host", "token"),
    [("10.0.0.10", None), ("10.0.0.10", ""), ("localhost", "tok"), ("127.0.0.1", "tok")],
    ids=["no-token", "empty-token", "localhost", "loopback"],
)
def test_no_token_or_a_loopback_host_means_no_remote_peers_exist(
    host: str, token: str | None
) -> None:
    assert remote(GATEWAY, host=host, token=token) == NO_REMOTE_INGRESS


@pytest.mark.parametrize("host", ["0.0.0.0", "::"])  # noqa: S104
@pytest.mark.parametrize("roles", [GATEWAY, STATION])
# meiva:note
# The wildcard literals are rejection fixtures, not binds.
def test_a_wildcard_reachable_host_fails_closed_with_or_without_a_token(
    roles: frozenset[str], host: str
) -> None:
    with pytest.raises(RuntimeError, match="reachable host is wildcard"):
        remote(roles, host=host)
    with pytest.raises(RuntimeError, match="reachable host is wildcard"):
        remote(roles, host=host, token=None)


def test_a_pure_runner_relays_to_its_gateway_and_a_remote_observatory_to_its_station() -> None:
    assert exporter_shape(RUNNER, observability_url_set=False) is ExporterShape.RUNNER_RELAY
    assert exporter_shape(RUNNER, observability_url_set=True) is ExporterShape.RUNNER_RELAY
    assert exporter_shape(GATEWAY, observability_url_set=True) is ExporterShape.STATION_RELAY
    assert exporter_shape(None, observability_url_set=True) is ExporterShape.STATION_RELAY


@pytest.mark.parametrize("roles", [GATEWAY, STATION, GATEWAY | RUNNER, None])
def test_everything_else_fans_out_to_its_local_backends(roles: frozenset[str] | None) -> None:
    assert exporter_shape(roles, observability_url_set=False) is ExporterShape.LOCAL_BACKENDS


def test_the_local_fan_out_appends_each_backends_otlp_path() -> None:
    assert fanout_bases(
        station_endpoint=None, loki_url="http://127.0.0.1:3100/", prometheus_url="http://h:9090"
    ) == ("http://127.0.0.1:3100/otlp", "http://h:9090/api/v1/otlp")


def test_a_remote_observatory_receives_every_signal_on_its_single_ingress() -> None:
    assert fanout_bases(
        station_endpoint="http://10.0.0.46:4318",
        loki_url="http://127.0.0.1:3100",
        prometheus_url="http://127.0.0.1:9090",
    ) == ("http://10.0.0.46:4318", "http://10.0.0.46:4318")


def exporters_of(shape: ExporterShape) -> dict[str, dict[str, object]]:
    block = render_exporters(
        shape,
        relay_endpoint="http://10.0.0.10:4318",
        authorization="Bearer tok",
        tempo_endpoint="http://127.0.0.1:14318/",
        bases=("http://127.0.0.1:3100/otlp", "http://127.0.0.1:9090/api/v1/otlp"),
    )
    return yaml.safe_load("exporters:" + block)["exporters"]


def test_the_local_shape_sends_each_signal_to_its_own_backend() -> None:
    exporters = exporters_of(ExporterShape.LOCAL_BACKENDS)

    assert {name: body["endpoint"] for name, body in exporters.items()} == {
        "otlphttp/tempo": "http://127.0.0.1:14318",
        "otlphttp/loki": "http://127.0.0.1:3100/otlp",
        "otlphttp/prometheus": "http://127.0.0.1:9090/api/v1/otlp",
    }
    assert all("headers" not in body for body in exporters.values())


@pytest.mark.parametrize("shape", [ExporterShape.RUNNER_RELAY, ExporterShape.STATION_RELAY])
def test_a_relay_shape_sends_every_signal_to_one_endpoint_with_the_bearer(
    shape: ExporterShape,
) -> None:
    exporters = exporters_of(shape)

    assert set(exporters) == {"otlphttp/tempo", "otlphttp/loki", "otlphttp/prometheus"}
    for body in exporters.values():
        assert body["endpoint"] == "http://10.0.0.10:4318"
        assert body["headers"] == {"Authorization": "Bearer tok"}


@pytest.mark.parametrize("shape", list(ExporterShape))
def test_every_exporter_queue_is_in_memory_and_drops_instead_of_blocking(
    shape: ExporterShape,
) -> None:
    for body in exporters_of(shape).values():
        queue = body["sending_queue"]
        assert isinstance(queue, dict)
        assert "storage" not in queue
        assert queue["block_on_overflow"] is False
        assert queue["wait_for_result"] is False


def test_values_are_quoted_for_yaml_and_for_ottl() -> None:
    assert yaml_quote("it's") == "'it''s'"
    assert ottl_quote('say "hi"') == '"say \\"hi\\""'
    assert ottl_quote("café") == '"café"'


def values() -> CollectorValues:
    return CollectorValues(
        meiva_home="/h",
        cluster_label=".meiva",
        machine_name='"box"',
        tempo_endpoint="http://t",
        loki_base="http://l",
        prom_base="http://p",
        retention_days=7,
        self_metrics_port=8888,
        local_ingress_endpoint="127.0.0.1:4318",
        data_plane_receivers="[DP]",
        data_plane_pipeline=", redis",
        exporters="[EXPORTERS]",
        remote=NO_REMOTE_INGRESS,
    )


def test_the_template_is_filled_from_the_named_values() -> None:
    template = (
        "home=$MEIVA_HOME label=$CLUSTER_LABEL machine=$MACHINE_NAME tempo=$TEMPO_ENDPOINT "
        "loki=$LOKI_BASE prom=$PROM_BASE days=$RETENTION_DAYS self=$SELF_METRICS_PORT "
        "otlp=$OTLP_RECEIVER_ENDPOINT dp=$DATA_PLANE_RECEIVERS|$DATA_PLANE_PIPELINE_RECEIVERS "
        "x=$OTLP_EXPORTERS r=[$REMOTE_OTLP_RECEIVER$CLUSTER_AUTH_EXTENSION]"
    )

    assert render_collector_config(template, values()) == (
        'home=/h label=.meiva machine="box" tempo=http://t loki=http://l prom=http://p days=7 '
        "self=8888 otlp=127.0.0.1:4318 dp=[DP]|, redis x=[EXPORTERS] r=[]"
    )


def test_a_placeholder_the_generator_does_not_supply_fails_loudly() -> None:
    with pytest.raises(KeyError, match="NOT_SUPPLIED"):
        render_collector_config("x=$NOT_SUPPLIED", values())


def test_an_unsupported_platform_is_skipped_with_a_warning_naming_it() -> None:
    step = plan_collector_install(
        tag=None,
        platform_label="linux i686",
        binary_present=False,
        marker=None,
        pinned_version="0.162.0",
    )

    assert isinstance(step, SkipUnsupportedPlatform)
    assert step.warning() == (
        "  ! otel-collector: no pinned otelcol-contrib for this platform "
        "(linux i686) — sidecar skipped"
    )


def test_a_binary_whose_marker_names_the_pin_is_kept() -> None:
    assert (
        plan_collector_install(
            tag="linux_amd64",
            platform_label="linux x86_64",
            binary_present=True,
            marker="0.162.0\n",
            pinned_version="0.162.0",
        )
        == KeepCollector()
    )


@pytest.mark.parametrize(
    ("binary_present", "marker"),
    [(False, "0.162.0"), (True, "0.100.0"), (True, None), (False, None)],
    ids=["no-binary", "stale-marker", "no-marker", "nothing"],
)
def test_a_missing_binary_or_a_stale_or_missing_marker_installs_the_pin(
    binary_present: bool, marker: str | None
) -> None:
    assert plan_collector_install(
        tag="linux_amd64",
        platform_label="linux x86_64",
        binary_present=binary_present,
        marker=marker,
        pinned_version="0.162.0",
    ) == InstallCollector("linux_amd64")


def test_a_gateway_home_that_is_not_the_station_skips_the_collector_and_says_why() -> None:
    warnings = plan_collector_step(
        gateway_role=True,
        collector_allowed=False,
        station_marker="/h/lgtm-host",
        residual_config=None,
    )

    assert warnings is not None
    [warning] = warnings
    assert "collector skipped" in warning
    assert "/h/lgtm-host is absent" in warning
    assert "MEIVA_TELEMETRY_OTLP_ENDPOINT" in warning


def test_a_residual_config_is_preserved_and_reported_when_the_collector_is_skipped() -> None:
    warnings = plan_collector_step(
        gateway_role=True,
        collector_allowed=False,
        station_marker="/h/lgtm-host",
        residual_config="/h/otel-collector/config.yaml",
    )

    assert warnings is not None
    assert warnings[1] == (
        "  ! otel-collector: stale/residual config remains at /h/otel-collector/config.yaml; "
        "collector files were preserved"
    )


@pytest.mark.parametrize(
    ("gateway_role", "collector_allowed"), [(False, False), (False, True), (True, True)]
)
def test_every_other_unit_installs_the_collector(
    gateway_role: bool, collector_allowed: bool
) -> None:
    assert (
        plan_collector_step(
            gateway_role=gateway_role,
            collector_allowed=collector_allowed,
            station_marker="/h/lgtm-host",
            residual_config="/h/otel-collector/config.yaml",
        )
        is None
    )
