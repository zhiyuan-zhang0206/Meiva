# meiva:note
# `meiva trace ship` against real transports: a loopback server that speaks the receiving end of
# OTLP/HTTP stands in for Tempo and for the gateway relay, the mirror is real files under
# `tmp_path`, and the configuration and machine role are values set on the command's own owners.
# Which target a role gets and which files and offsets are replayed are decided in
# `plans/trace_ship_*.py` and covered in `test_trace_ship_plans.py`; this file covers the shell that
# reads the mirror, POSTs and keeps the watermark.

from __future__ import annotations

import gzip
import json
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from google.protobuf.json_format import MessageToDict
from opentelemetry.exporter.otlp.proto.common.trace_encoder import encode_spans
from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExporter, SpanExportResult

from base.host.db_authority.api import telemetry_token
from cli.commands.observability import trace
from cli.commands.observability.plans.trace_ship.files import ship_window
from cli.commands.observability.plans.trace_ship.target import ShipTarget, TraceShipError
from cli.commands.observability.tests.observability_inputs import (
    REPO_ROOT,
    install_unit_capability,
    observability,
)
from cli.commands.observability.tests.scripted_http import Reply, Served, serve
from cli.database.runtime import CommandRuntime
from cli.database.tests.command_inputs import command_runtime as command_runtime

CHILD = Path(__file__).with_name("_ship_proxy_child.py")
ROTATED = "spans-2026-08-27T03-29-10.942-size.jsonl"
WATERMARK = ".ship-watermark.json"
OTLP_PATH = "/v1/traces"


class _Capture(SpanExporter):
    def __init__(self) -> None:
        self.spans: list[ReadableSpan] = []

    def export(self, spans: Sequence[ReadableSpan]) -> SpanExportResult:
        self.spans.extend(spans)
        return SpanExportResult.SUCCESS

    def shutdown(self) -> None:
        return None


# meiva:note
# One OTLP-JSON `ExportTraceServiceRequest` line carrying a span called `span_name`, exactly as the
# collector's file exporter writes it.
def _otlp_line(span_name: str) -> str:
    capture = _Capture()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(capture))
    with provider.get_tracer("t").start_as_current_span(span_name):
        pass
    return json.dumps(MessageToDict(encode_spans(capture.spans)), separators=(",", ":"))


def _lines(*names: str) -> str:
    return "".join(_otlp_line(name) + "\n" for name in names)


def _span_names(body: bytes) -> list[str]:
    request = ExportTraceServiceRequest()
    request.ParseFromString(body)
    return [
        span.name
        for resource_spans in request.resource_spans
        for scope_spans in resource_spans.scope_spans
        for span in scope_spans.spans
    ]


def _posted_span_names(served: Served) -> list[str]:
    return [name for received in served.received for name in _span_names(received.body)]


def _write_gz(path: Path, text: str) -> None:
    with gzip.open(path, "wt", encoding="utf-8") as stream:
        stream.write(text)


@pytest.fixture
def traces(tmp_path: Path) -> Path:
    path = tmp_path / "traces"
    path.mkdir()
    return path


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


# meiva:note
# Point the command's configuration at `tempo_url` with the OTLP surface on, and give the machine
# `roles` (a gateway by default: it replays straight to Tempo).
def _configure(command_runtime: CommandRuntime, tempo_url: str, *, roles: str = "gateway") -> None:
    command_runtime.config.set_field("telemetry_otlp_enabled", True)
    command_runtime.config.set_field("telemetry_tempo_endpoint", tempo_url)
    command_runtime.machine.set(role=roles)


def _ship(
    command_runtime: CommandRuntime,
    traces: Path,
    home: Path,
    *,
    since: str | None = None,
    until: str | None = None,
    dry_run: bool = False,
) -> int:
    return trace.cmd_trace_ship(
        since=since,
        until=until,
        dry_run=dry_run,
        machine=command_runtime.machine,
        traces_dir=traces,
        config=command_runtime.config,
        home=home,
    )


def test_load_watermark_drops_entries_for_missing_files(traces: Path) -> None:
    (traces / "spans.jsonl").write_text("x\n", encoding="utf-8")
    (traces / WATERMARK).write_text(
        json.dumps({"spans.jsonl": 2, "spans-2020-01-01T00-00-00.000-size.jsonl": 5}),
        encoding="utf-8",
    )

    assert trace.load_watermark(traces_dir=traces) == {"spans.jsonl": 2}


# meiva:note
# OTLP/JSON ids are hex strings even though protobuf JSON treats bytes as base64, so the line must
# reach the wire with the ids as the raw bytes of their hex.
def test_post_line_sends_otlp_json_hex_ids_as_protobuf_bytes() -> None:
    trace_id = "0123456789abcdef0123456789abcdef"
    span_id = "0123456789abcdef"
    line = json.dumps(
        {
            "resourceSpans": [
                {
                    "scopeSpans": [
                        {
                            "spans": [
                                {"traceId": trace_id, "spanId": span_id, "parentSpanId": span_id}
                            ]
                        }
                    ]
                }
            ]
        }
    )

    with serve({OTLP_PATH: Reply()}) as tempo, httpx.Client(timeout=10.0) as client:
        sent = trace.post_line(
            client,
            ShipTarget(endpoint=f"{tempo.url}{OTLP_PATH}", headers={}, label="tempo"),
            line,
        )

    assert sent == 1
    [received] = tempo.received
    request = ExportTraceServiceRequest()
    request.ParseFromString(received.body)
    span = request.resource_spans[0].scope_spans[0].spans[0]
    assert span.trace_id == bytes.fromhex(trace_id)
    assert span.span_id == bytes.fromhex(span_id)
    assert span.parent_span_id == bytes.fromhex(span_id)
    assert received.headers["content-type"] == "application/x-protobuf"


def test_post_line_raises_when_the_backend_refuses_the_batch() -> None:
    with serve({OTLP_PATH: Reply(status=503)}) as tempo, httpx.Client(timeout=10.0) as client:
        target = ShipTarget(endpoint=f"{tempo.url}{OTLP_PATH}", headers={}, label="tempo")
        with pytest.raises(httpx.HTTPStatusError):
            trace.post_line(client, target, _otlp_line("a"))


def test_ship_refuses_when_the_otlp_kill_switch_is_off(
    command_runtime: CommandRuntime, traces: Path, home: Path
) -> None:
    command_runtime.config.set_field("telemetry_otlp_enabled", False)
    command_runtime.machine.set(role="gateway")

    with pytest.raises(TraceShipError, match="MEIVA_TELEMETRY_OTLP_ENABLED"):
        _ship(command_runtime, traces, home)


# meiva:note
# A dry run reads the mirror only: no backend configuration is needed (the kill switch may be off,
# which is exactly when the mirror is worth inspecting), nothing is POSTed and the watermark is
# untouched.
@pytest.mark.parametrize("otlp_enabled", [True, False])
def test_dry_run_counts_the_mirror_and_posts_nothing(
    command_runtime: CommandRuntime,
    traces: Path,
    home: Path,
    capsys: pytest.CaptureFixture[str],
    *,
    otlp_enabled: bool,
) -> None:
    (traces / "spans.jsonl").write_text(_lines("a", "b"), encoding="utf-8")
    (traces / "spans-2026-06-16T00-00-00.000-size.jsonl").write_text(_lines("c"), encoding="utf-8")

    with serve({OTLP_PATH: Reply()}) as tempo:
        _configure(command_runtime, tempo.url)
        command_runtime.config.set_field("telemetry_otlp_enabled", otlp_enabled)

        assert _ship(command_runtime, traces, home, dry_run=True) == 0

    assert tempo.received == []
    assert not (traces / WATERMARK).exists()
    out = capsys.readouterr().out
    assert "would ship (incremental): 2 files, 3 batches" in out
    assert "target: (dry-run" in out


def test_incremental_ship_posts_each_line_and_advances_the_watermark(
    command_runtime: CommandRuntime, traces: Path, home: Path
) -> None:
    mirror = traces / "spans.jsonl"
    mirror.write_text(_lines("a", "b"), encoding="utf-8")

    with serve({OTLP_PATH: Reply()}) as tempo:
        _configure(command_runtime, tempo.url)

        assert _ship(command_runtime, traces, home) == 0

        assert [_span_names(received.body) for received in tempo.received] == [["a"], ["b"]]
        marks = json.loads((traces / WATERMARK).read_text(encoding="utf-8"))
        assert marks == {"spans.jsonl": mirror.stat().st_size}

        tempo.received.clear()
        assert _ship(command_runtime, traces, home) == 0
        assert tempo.received == []

        mirror.write_text(mirror.read_text(encoding="utf-8") + _lines("c"), encoding="utf-8")
        assert _ship(command_runtime, traces, home) == 0
        assert _posted_span_names(tempo) == ["c"]


# meiva:note
# A backend that refuses a batch stops the ship with the watermark left just after the last
# accepted line, so the next run resumes at the refused line: nothing is re-sent, nothing skipped.
def test_a_refused_batch_leaves_the_watermark_after_the_last_accepted_line(
    command_runtime: CommandRuntime, traces: Path, home: Path
) -> None:
    first = _otlp_line("a") + "\n"
    (traces / "spans.jsonl").write_text(first + _lines("b", "c"), encoding="utf-8")

    with serve({OTLP_PATH: [Reply(), Reply(status=503)]}) as tempo:
        _configure(command_runtime, tempo.url)

        with pytest.raises(httpx.HTTPStatusError):
            _ship(command_runtime, traces, home)

    assert json.loads((traces / WATERMARK).read_text(encoding="utf-8")) == {
        "spans.jsonl": len(first.encode())
    }
    with serve({OTLP_PATH: Reply()}) as tempo:
        _configure(command_runtime, tempo.url)

        assert _ship(command_runtime, traces, home) == 0

        assert _posted_span_names(tempo) == ["b", "c"]


def test_incremental_ship_reads_gzipped_segments_and_keys_the_watermark_by_base_name(
    command_runtime: CommandRuntime, traces: Path, home: Path
) -> None:
    _write_gz(traces / f"{ROTATED}.gz", _lines("a", "b"))

    with serve({OTLP_PATH: Reply()}) as tempo:
        _configure(command_runtime, tempo.url)

        assert _ship(command_runtime, traces, home) == 0

        assert _posted_span_names(tempo) == ["a", "b"]
        marks = json.loads((traces / WATERMARK).read_text(encoding="utf-8"))
        assert marks[ROTATED] > 0
        assert f"{ROTATED}.gz" not in marks

        tempo.received.clear()
        assert _ship(command_runtime, traces, home) == 0
        assert tempo.received == []


def test_incremental_ship_of_a_gzipped_segment_resumes_from_a_partial_watermark(
    command_runtime: CommandRuntime, traces: Path, home: Path
) -> None:
    first = _otlp_line("a") + "\n"
    _write_gz(traces / f"{ROTATED}.gz", first + _lines("b"))
    (traces / WATERMARK).write_text(json.dumps({ROTATED: len(first.encode())}), encoding="utf-8")

    with serve({OTLP_PATH: Reply()}) as tempo:
        _configure(command_runtime, tempo.url)

        assert _ship(command_runtime, traces, home) == 0

    assert _posted_span_names(tempo) == ["b"]


def test_windowed_ship_includes_gzipped_segments(
    command_runtime: CommandRuntime, traces: Path, home: Path
) -> None:
    _write_gz(traces / "spans-2026-06-16T00-00-00.000-size.jsonl.gz", _lines("c"))
    (traces / "spans-2020-06-16T00-00-00.000-size.jsonl").write_text(_lines("x"), encoding="utf-8")

    with serve({OTLP_PATH: Reply()}) as tempo:
        _configure(command_runtime, tempo.url)

        assert (
            _ship(
                command_runtime,
                traces,
                home,
                since="2026-06-15",  # time-bomb-ok: explicit request range under test
                until="2026-06-17",  # time-bomb-ok: explicit request range under test
            )
            == 0
        )

    assert _posted_span_names(tempo) == ["c"]


def test_windowed_ship_filters_by_file_day_and_ignores_the_watermark(
    command_runtime: CommandRuntime, traces: Path, home: Path
) -> None:
    in_window = "spans-2026-06-16T03-04-05.000-size.jsonl"
    (traces / "spans-2026-06-10T00-00-00.000-size.jsonl").write_text(
        _lines("old"), encoding="utf-8"
    )
    (traces / in_window).write_text(_lines("new"), encoding="utf-8")
    (traces / WATERMARK).write_text(json.dumps({in_window: 99999}), encoding="utf-8")

    with serve({OTLP_PATH: Reply()}) as tempo:
        _configure(command_runtime, tempo.url)

        assert (
            _ship(
                command_runtime,
                traces,
                home,
                since="2026-06-15",  # time-bomb-ok: explicit request range under test
                until="2026-06-17",  # time-bomb-ok: explicit request range under test
            )
            == 0
        )

    assert _posted_span_names(tempo) == ["new"]
    assert json.loads((traces / WATERMARK).read_text(encoding="utf-8")) == {in_window: 99999}


# meiva:note
# A gateway replays straight to its loopback Tempo and bypasses its own collector, because that
# collector would mirror the replay back into the files being shipped. Tempo needs no credential.
@pytest.mark.parametrize("roles", ["gateway", "gateway,agent-runner"])
def test_gateway_ship_posts_to_tempo_without_a_credential(
    command_runtime: CommandRuntime, traces: Path, home: Path, *, roles: str
) -> None:
    (traces / "spans.jsonl").write_text(_lines("a", "b"), encoding="utf-8")

    with serve({OTLP_PATH: Reply()}) as tempo:
        _configure(command_runtime, tempo.url, roles=roles)

        assert _ship(command_runtime, traces, home) == 0

    assert [(received.method, received.path) for received in tempo.received] == [
        ("POST", OTLP_PATH),
        ("POST", OTLP_PATH),
    ]
    for received in tempo.received:
        assert "authorization" not in received.headers
        assert received.headers["content-type"] == "application/x-protobuf"


# meiva:note
# A pure runner dials the gateway's relay receiver with the telemetry token of its installed
# capability and holds no human secret. The published relay must be a non-loopback address, so the
# target is resolved against such an address and the wire leg runs against a loopback server that
# stands in for the relay and is handed the same headers.
def test_runner_ship_presents_the_telemetry_token_of_its_capability(
    command_runtime: CommandRuntime, traces: Path, home: Path
) -> None:
    install_unit_capability(home, telemetry="unit-telemetry-token-" + "x" * 16)
    command_runtime.machine.set(role="agent-runner")
    (traces / "spans.jsonl").write_text(_lines("remote"), encoding="utf-8")

    target = trace.resolve_ship_target(
        observability(gateway_otlp_endpoint="http://10.0.0.10:4318"),
        cluster_secret="",
        machine=command_runtime.machine,
        home=home,
    )

    assert target == ShipTarget(
        endpoint="http://10.0.0.10:4318/v1/traces",
        headers={"Authorization": "Bearer unit-telemetry-token-" + "x" * 16},
        label="gateway OTLP relay",
    )
    with serve({OTLP_PATH: Reply()}) as relay:
        trace.ship_files(
            [traces / "spans.jsonl"],
            {},
            target=replace(target, endpoint=f"{relay.url}{OTLP_PATH}"),
            window=ship_window(None, None),
            traces_dir=traces,
        )

    [received] = relay.received
    assert received.headers["authorization"] == "Bearer unit-telemetry-token-" + "x" * 16
    assert _span_names(received.body) == ["remote"]


def test_runner_without_a_capability_derives_the_relay_token_from_the_secret_it_holds(
    command_runtime: CommandRuntime, home: Path
) -> None:
    command_runtime.machine.set(role="agent-runner")
    held = "cluster-credential"

    target = trace.resolve_ship_target(
        observability(gateway_otlp_endpoint="http://10.0.0.10:4318"),
        cluster_secret=held,
        machine=command_runtime.machine,
        home=home,
    )

    assert target.headers == {"Authorization": f"Bearer {telemetry_token(held)}"}


def test_runner_holding_neither_a_capability_nor_a_secret_cannot_ship(
    command_runtime: CommandRuntime, traces: Path, home: Path
) -> None:
    command_runtime.config.set_field("telemetry_otlp_enabled", True)
    command_runtime.config.set_field("gateway_otlp_endpoint", "http://10.0.0.10:4318")
    command_runtime.config.set_field("cluster_secret", "")
    command_runtime.machine.set(role="agent-runner")
    (traces / "spans.jsonl").write_text(_lines("remote"), encoding="utf-8")

    with pytest.raises(TraceShipError, match="holds no capability"):
        _ship(command_runtime, traces, home)

    assert not (traces / WATERMARK).exists()


def _proxy_environment(proxy_url: str, home: Path) -> dict[str, str]:
    environment = {"MEIVA_HOME": str(home), "NO_PROXY": "", "no_proxy": ""}
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        environment[name] = proxy_url
        environment[name.lower()] = proxy_url
    return environment


def _run_child(environment: dict[str, str], *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(CHILD), *args],
        cwd=REPO_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


# meiva:note
# The ship client must never route through a host proxy. The macOS system proxy (a VPN or clash on
# 127.0.0.1) handles JSON but mangles protobuf bodies and answers 502; it broke the trace replay
# pipeline for two days on 2026-08-25. The proxy variables below send a default httpx client to a
# proxy that answers 502 (the control run shows they do), and the ship client still reaches the
# target directly.
def test_ship_client_bypasses_the_environment_proxy(traces: Path, home: Path) -> None:
    (traces / "spans.jsonl").write_text(_lines("a"), encoding="utf-8")
    with serve({OTLP_PATH: Reply()}) as tempo:
        target_url = f"{tempo.url}{OTLP_PATH}"
        with serve({target_url: Reply(status=502)}) as proxy:
            environment = _proxy_environment(proxy.url, home)

            control = _run_child(environment, "control", target_url)
            assert control.returncode == 0, control.stderr
            assert control.stdout.strip() == "502"
            assert [received.method for received in proxy.received] == ["POST"]
            assert tempo.received == []

            proxy.received.clear()
            shipped = _run_child(environment, "ship", str(traces), target_url)
            assert shipped.returncode == 0, shipped.stderr

    assert proxy.received == []
    assert _posted_span_names(tempo) == ["a"]
