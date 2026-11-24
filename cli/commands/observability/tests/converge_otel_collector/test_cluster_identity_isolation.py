# meiva:link docs/decisions/observability/telemetry/2026-11-24-cluster-routing-identity.md

from __future__ import annotations

import dataclasses
import json
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
import yaml

from base.cluster import cluster_id
from base.host.db_authority.api import ensure_telemetry_token
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    GATEWAY,
    GATEWAY_INGRESS,
    RUNNER,
    CollectorUnit,
    mapping_at,
    sequence_at,
)
from cli.commands.observability.tests.converge_otel_collector.collector_harness import (
    collector_unit as collector_unit,
)
from cli.commands.observability.tests.observability_inputs import (
    TELEMETRY_TOKEN,
    install_unit_capability,
    observability,
    write_cluster_record,
)
from cli.database.tests.command_inputs import command_runtime as command_runtime

OTHER_TOKEN = "telemetry-" + "o" * 40
_ID_PATTERN = re.compile(r'IsMatch\(resource\.attributes\["meiva\.cluster"\], "([^"]+)"\)')


def _unit_with_token(unit: CollectorUnit, parent: str, token: str) -> CollectorUnit:
    home = unit.home.parent / parent / ".meiva"
    home.parent.mkdir()
    write_cluster_record(home)
    ensure_telemetry_token(home.resolve(), token)
    return dataclasses.replace(unit, home=home)


def _routing_pattern(unit: CollectorUnit, roles: frozenset[str] = GATEWAY) -> str:
    config = unit.render(roles, observability=observability(gateway_otlp_endpoint=GATEWAY_INGRESS))
    conditions = sequence_at(
        mapping_at(config, "processors", "filter/cluster_allow"), "logs", "log_record"
    )
    match = _ID_PATTERN.search(str(conditions[0]))
    assert match is not None
    return match.group(1)


def test_same_named_homes_of_different_clusters_filter_each_others_records(
    collector_unit: CollectorUnit,
) -> None:
    home_a = _unit_with_token(collector_unit, "a", TELEMETRY_TOKEN)
    home_b = _unit_with_token(collector_unit, "b", OTHER_TOKEN)
    assert home_a.home.name == home_b.home.name == ".meiva"

    pattern_a, pattern_b = _routing_pattern(home_a), _routing_pattern(home_b)

    assert re.search(pattern_a, cluster_id(TELEMETRY_TOKEN))
    assert re.search(pattern_b, cluster_id(OTHER_TOKEN))
    assert not re.search(pattern_a, cluster_id(OTHER_TOKEN))
    assert not re.search(pattern_b, cluster_id(TELEMETRY_TOKEN))


def test_every_unit_of_one_cluster_routes_under_the_same_identity(
    collector_unit: CollectorUnit,
) -> None:
    gateway = _unit_with_token(collector_unit, "gateway", TELEMETRY_TOKEN)
    runner_home = collector_unit.home.parent / "runner" / "not-named-meiva"
    runner_home.parent.mkdir()
    install_unit_capability(runner_home, telemetry=TELEMETRY_TOKEN)
    runner = dataclasses.replace(collector_unit, home=runner_home)

    assert _routing_pattern(runner, RUNNER) == _routing_pattern(gateway)


def test_a_unit_without_a_telemetry_token_admits_any_routing_identity(
    collector_unit: CollectorUnit,
) -> None:
    collector_unit.retract_token()

    pattern = _routing_pattern(collector_unit)

    assert pattern == ".*"
    assert re.search(pattern, cluster_id(OTHER_TOKEN))


class _Capture(BaseHTTPRequestHandler):
    bodies: list[bytes] = []  # noqa: RUF012 — one capture server per test process

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.bodies.append(self.rfile.read(length))
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b"{}")

    def log_message(self, format: str, *args: object) -> None:
        del format, args


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _post_log(port: int, resource: dict[str, str], marker: str) -> None:
    payload = {
        "resourceLogs": [
            {
                "resource": {
                    "attributes": [
                        {"key": k, "value": {"stringValue": v}} for k, v in resource.items()
                    ]
                },
                "scopeLogs": [
                    {
                        "logRecords": [
                            {
                                "timeUnixNano": str(time.time_ns()),
                                "body": {"stringValue": marker},
                            }
                        ]
                    }
                ],
            }
        ]
    }
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}/v1/logs",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(request, timeout=10).read()  # noqa: S310


@pytest.mark.skipif(
    not Path(os.environ.get("MEIVA_TEST_OTELCOL_BINARY", "")).is_file(),
    reason="set MEIVA_TEST_OTELCOL_BINARY to an otelcol-contrib binary to run the collector",
)
def test_the_real_collector_drops_the_other_clusters_records_and_counts_them(
    collector_unit: CollectorUnit, tmp_path: Path
) -> None:
    binary = os.environ["MEIVA_TEST_OTELCOL_BINARY"]
    rendered = collector_unit.render(GATEWAY)
    capture = HTTPServer(("127.0.0.1", 0), _Capture)
    _Capture.bodies = []
    threading.Thread(target=capture.serve_forever, daemon=True).start()
    ingress, metrics = _free_port(), _free_port()
    config = {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": f"127.0.0.1:{ingress}"}}}},
        "processors": {
            "filter/cluster_allow": mapping_at(rendered, "processors", "filter/cluster_allow")
        },
        "exporters": {
            "otlphttp": {
                "endpoint": f"http://127.0.0.1:{capture.server_port}",
                "compression": "none",
                "retry_on_failure": {"enabled": False},
            }
        },
        "service": {
            "telemetry": {
                "metrics": {
                    "readers": [
                        {
                            "pull": {
                                "exporter": {"prometheus": {"host": "127.0.0.1", "port": metrics}}
                            }
                        }
                    ]
                }
            },
            "pipelines": {
                "logs": {
                    "receivers": ["otlp"],
                    "processors": ["filter/cluster_allow"],
                    "exporters": ["otlphttp"],
                }
            },
        },
    }
    config_path = tmp_path / "otelcol.yaml"
    config_path.write_text(yaml.safe_dump(config))
    process = subprocess.Popen([binary, "--config", str(config_path)])
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            try:
                socket.create_connection(("127.0.0.1", ingress), timeout=1).close()
                break
            except OSError:
                time.sleep(0.2)
        own = {"cluster": ".meiva", "meiva.cluster": cluster_id(TELEMETRY_TOKEN)}
        foreign = {"cluster": ".meiva", "meiva.cluster": cluster_id(OTHER_TOKEN)}
        legacy = {"cluster": ".meiva"}
        for marker, resource in (("own", own), ("foreign", foreign), ("legacy", legacy)):
            _post_log(ingress, resource, marker)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and len(_Capture.bodies) < 2:
            time.sleep(0.2)
        time.sleep(1)
        texts = [_decode_markers(body) for body in _Capture.bodies]
        markers = {m for found in texts for m in found}
        assert markers == {"own", "legacy"}
        scrape = urllib.request.urlopen(f"http://127.0.0.1:{metrics}/metrics", timeout=10).read()
        assert re.search(
            rb'otelcol_processor_filter_logs_filtered\{[^}]*filter="filter/cluster_allow"[^}]*\} 1',
            scrape,
        )
    finally:
        process.terminate()
        process.wait(timeout=30)
        capture.shutdown()


def _decode_markers(body: bytes) -> set[str]:
    return {m.decode() for m in re.findall(rb"own|foreign|legacy", body)}
