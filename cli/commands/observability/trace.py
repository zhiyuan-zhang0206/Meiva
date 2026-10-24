# meiva:note
# `meiva trace ship`: replay the local trace mirror (collector JSONL) to Tempo.
#
# Recording (`base/telemetry/tracing.py`) exports spans over OTLP/HTTP to the local OTel Collector
# sidecar, whose file exporter mirrors them to `$MEIVA_HOME/traces/`: the active `spans.jsonl`
# plus rotated `spans-<ISO-timestamp>.jsonl` backups, each line a standard OTLP/JSON
# `ExportTraceServiceRequest`. This command is the RECOVERY consumer: it reads the mirror and
# POSTs each line back over OTLP/HTTP (protobuf). Live delivery is the collector's job, with a
# queue for backend outages; ship exists for the gaps the queue could not hold (backend down
# longer than the queue, offline machines, re-import of a past window). Replaying through the
# sidecar would write the replayed lines back into the mirror and loop the watermark, so ship dials
# Tempo directly on a gateway, or the gateway's relay receiver on a pure runner.
#
# The target, the file selection and the watermark decisions are in `plans/trace_ship_*.py`; this
# module reads the settings and the mirror, POSTs and keeps the watermark.
# meiva:link base/telemetry/otlp/docs/telemetry-otlp/trace-mirror.meiva.okf.md

from __future__ import annotations

import gzip
import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx

from base.cluster.machine import MachineIdentity
from base.config import ConfigBoot
from base.host.db_authority.unit import telemetry_bearer
from base.telemetry.trace_mirror import mirror_day, mirror_sort_key
from cli.commands.observability.inputs import observability_config_from_boot
from cli.commands.observability.plans.observability_config import ObservabilityConfig
from cli.commands.observability.plans.trace_ship.files import (
    ShipTotals,
    ShipWindow,
    file_report_line,
    live_watermarks,
    plan_file_cursor,
    repaired_span_id,
    ship_summary_lines,
    ship_window,
)
from cli.commands.observability.plans.trace_ship.target import (
    ShipTarget,
    plan_ship_target,
)
from cli.commands.observability.url_split import split_url

_WATERMARK_NAME = ".ship-watermark.json"
_SPAN_ID_LENGTHS = (("trace_id", 16), ("span_id", 8), ("parent_span_id", 8))


# meiva:note
# The role-correct replay target.
#
# A pure runner presents the telemetry token of its capability; every other role replays straight
# to its loopback Tempo. The token is read only for a runner: a gateway derives it from the
# secret, so the capability file is not consulted for a role that does not need it.
def resolve_ship_target(
    observability: ObservabilityConfig,
    *,
    cluster_secret: str,
    machine: MachineIdentity,
    home: Path,
) -> ShipTarget:
    roles = machine.role()
    runner = roles == frozenset({"agent-runner"})
    gateway = observability.gateway_otlp_endpoint.strip()
    return plan_ship_target(
        otlp_enabled=observability.telemetry_otlp_enabled,
        roles=roles,
        tempo_endpoint=observability.telemetry_tempo_endpoint,
        gateway_endpoint=gateway,
        gateway=split_url(gateway),
        token=telemetry_bearer(home.resolve(), cluster_secret) if runner else None,
    )


# meiva:note
# Day stamp of a mirror file: the collector's rotated `spans-<ISO-timestamp>-(size|time).jsonl`
# names (`.gz` suffix tolerated), else the file's mtime in UTC, the same clock the name day stamps
# and `--since` / `--until` use. The active `spans.jsonl` carries no stamp: its content is today's.
def _file_day(path: Path) -> date:
    day = mirror_day(path)
    if day is not None:
        return day
    return datetime.fromtimestamp(path.stat().st_mtime, tz=UTC).date()


# meiva:note
# The byte offsets recorded for the mirror files that still exist.
def load_watermark(*, traces_dir: Path) -> dict[str, int]:
    path = traces_dir / _WATERMARK_NAME
    if not path.exists():
        return {}
    marks: dict[str, int] = json.loads(path.read_text(encoding="utf-8"))
    return live_watermarks(marks, present=frozenset(entry.name for entry in traces_dir.iterdir()))


def _save_watermark(marks: dict[str, int], *, traces_dir: Path) -> None:
    path = traces_dir / _WATERMARK_NAME
    path.write_text(json.dumps(marks, indent=2, sort_keys=True), encoding="utf-8")


# meiva:note
# Repair ids stored as base64 of hex (the wrong length) back to their raw byte length, in place.
def _correct_span_ids(request: Any) -> None:
    for resource_spans in request.resource_spans:
        for scope_spans in resource_spans.scope_spans:
            for span in scope_spans.spans:
                for attribute, expected_length in _SPAN_ID_LENGTHS:
                    repaired = repaired_span_id(getattr(span, attribute), expected_length)
                    if repaired is not None:
                        setattr(span, attribute, repaired)


# meiva:note
# POST one OTLP/JSON line as protobuf and return the span count in it.
#
# Reconstructs the OTLP `ExportTraceServiceRequest` from the stored JSON and re-serializes it to
# protobuf: the exact wire shape Tempo's OTLP endpoint ingests.
#
# The protobuf modules are imported here: only a real ship needs them, and a dry run must not pay
# for them.
def post_line(client: httpx.Client, target: ShipTarget, line: str) -> int:
    from google.protobuf.json_format import Parse
    from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

    request = Parse(line, ExportTraceServiceRequest())
    _correct_span_ids(request)
    headers = {"Content-Type": "application/x-protobuf", **target.headers}
    response = client.post(target.endpoint, content=request.SerializeToString(), headers=headers)
    response.raise_for_status()
    return sum(len(ss.spans) for rs in request.resource_spans for ss in rs.scope_spans)


# meiva:note
# Read and discard `count` bytes from a stream that cannot seek (gzip).
def _discard(stream: gzip.GzipFile, count: int) -> None:
    remaining = count
    while remaining > 0:
        chunk = stream.read(min(remaining, 1024 * 1024))
        if not chunk:
            break
        remaining -= len(chunk)


# meiva:note
# Ship one mirror file and return its (batches, spans).
#
# Incremental mode starts at the watermark offset and advances the watermark per line, so a crash
# never re-sends or skips. Windowed mode ships whole files; re-imports are idempotent, so the
# watermark is untouched. Binary mode: seek and offset are true byte positions, so an interrupted
# ship resumes exactly after the last POSTed line. A gzip stream cannot seek, so the shipped
# prefix is read and discarded: the cost of a fully shipped segment is decompression only, never
# a re-POST. A dry run counts lines and POSTs nothing.
def _ship_file(
    path: Path,
    *,
    client: httpx.Client | None,
    target: ShipTarget | None,
    marks: dict[str, int],
    window: ShipWindow,
    traces_dir: Path,
) -> tuple[int, int] | None:
    cursor = plan_file_cursor(
        path.name,
        windowed=window.windowed,
        marks=marks,
        size=0 if path.name.endswith(".gz") else path.stat().st_size,
    )
    if cursor.shipped_out:
        return None
    batches = 0
    spans = 0
    with gzip.open(path, "rb") if cursor.gzipped else path.open("rb") as stream:
        if isinstance(stream, gzip.GzipFile):
            _discard(stream, cursor.start)
        else:
            stream.seek(cursor.start)
        offset = cursor.start
        for raw in stream:
            offset += len(raw)
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            if client is not None and target is not None:
                spans += post_line(client, target, line)
            batches += 1
            if client is not None and not window.windowed:
                marks[cursor.key] = offset
                _save_watermark(marks, traces_dir=traces_dir)
    return batches, spans


# meiva:note
# Ship the files and return the totals.
#
# The client does not trust the environment: the macOS system proxy (a VPN or clash on
# 127.0.0.1) would otherwise intercept the protobuf POSTs to a tailnet Tempo endpoint and answer
# 502, because the proxy handles JSON but mangles binary bodies. `NO_PROXY` does not bypass the
# macOS system-config branch httpx reads, so the cross-machine ops RPC does the same.
def ship_files(
    files: list[Path],
    marks: dict[str, int],
    *,
    target: ShipTarget | None,
    window: ShipWindow,
    traces_dir: Path,
) -> ShipTotals:
    shipped_files = 0
    total_batches = 0
    total_spans = 0
    with httpx.Client(timeout=30.0, trust_env=False) as client:
        for path in files:
            shipped = _ship_file(
                path,
                client=client if target is not None else None,
                target=target,
                marks=marks,
                window=window,
                traces_dir=traces_dir,
            )
            if shipped is None or not shipped[0]:
                continue
            shipped_files += 1
            total_batches += shipped[0]
            total_spans += shipped[1]
            print(file_report_line(path.name, batches=shipped[0], spans=shipped[1]))
    return ShipTotals(shipped_files, total_batches, total_spans)


# meiva:note
# Ship the local trace mirror toward Tempo over the role-correct transport.
#
# With no `--since` / `--until`, ships incrementally from the per-file watermark. With a window,
# ships the matching files whole and leaves the watermark alone. `dry_run` reads the mirror only
# and needs no backend configuration: it is the one way to inspect what reached the mirror when
# the backend is off. The mirror is `spans.jsonl` plus rotated `spans-<ts>-(size|time).jsonl` and
# gzipped old segments, oldest first with the active file last.
def cmd_trace_ship(
    *,
    since: str | None,
    until: str | None,
    dry_run: bool,
    machine: MachineIdentity,
    traces_dir: Path,
    config: ConfigBoot,
    home: Path,
) -> int:
    target = None
    if not dry_run:
        view = config.view
        target = resolve_ship_target(
            observability_config_from_boot(config),
            cluster_secret=view.data_plane.cluster_secret,
            machine=machine,
            home=home,
        )
    window = ship_window(since, until)
    files = sorted(traces_dir.glob("spans*.jsonl*"), key=mirror_sort_key)
    if window.windowed:
        files = [path for path in files if window.contains(_file_day(path))]
    totals = ship_files(
        files,
        load_watermark(traces_dir=traces_dir),
        target=target,
        window=window,
        traces_dir=traces_dir,
    )
    target_line = (
        "target: (dry-run — role-correct target not resolved)"
        if target is None
        else f"target: {target.label} -> {target.endpoint}"
    )
    for line in ship_summary_lines(
        dry_run=dry_run, scope=window.scope, totals=totals, target_line=target_line
    ):
        print(line)
    return 0
