#!/usr/bin/env python3
"""Fetch one OTel trace as normalized JSON from Tempo.

Two modes:

- **Find** (`--search <traceql>`): query Tempo's search API and list matching
  traces (id, duration, matched span count). Run this first to get trace ids.
- **Fetch** (`--trace-id <hex-32>`): pull one trace's full span set from
  `GET /api/traces/{id}` and write `trace_raw.json` for `read_trace.py`. Tempo
  refuses a trace above its 5 MB cap ("trace exceeds max size"); there is no
  local copy of spans to fall back on, so narrow with `--search` instead.

Output `trace_raw.json` (contract for read_trace.py):

    {
      "trace_id": "<hex-32>",
      "source": "tempo",
      "fetched_at": "<ISO-8601>",
      "spans": [
        {
          "span_id": "<hex-16>",
          "parent_span_id": "<hex-16>" | null,
          "name": str,
          "kind": "INTERNAL" | "CLIENT" | "SERVER" | "PRODUCER" | "CONSUMER" | null,
          "start_ns": int, "end_ns": int,
          "status": "OK" | "ERROR" | null,
          "attributes": {key: scalar}
        }
      ]
    }

Stdlib only; runs under any Python 3.12.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

_KIND_STRIP = "SPAN_KIND_"
_STATUS_OK = "STATUS_CODE_OK"
_STATUS_ERROR = "STATUS_CODE_ERROR"


def _anyvalue(value: dict) -> object:
    """Collapse a proto3-JSON AnyValue dict to a plain scalar/list."""
    for key in ("stringValue", "intValue", "doubleValue", "boolValue"):
        if key in value:
            return value[key]
    if "arrayValue" in value:
        return [_anyvalue(v) for v in value["arrayValue"].get("values", [])]
    if "kvlistValue" in value:
        return {
            f["key"]: _anyvalue(f["value"])
            for f in value["kvlistValue"].get("values", [])
            if "key" in f and "value" in f
        }
    return None


def _id_to_hex(value: str) -> str:
    """Normalize a trace/span id field to lowercase hex.

    Tempo carries ids as hex strings today; OTLP/JSON envelopes may carry
    base64 per the OTLP JSON spec. Detect by shape: hex ids are
    32 (trace) or 16 (span) chars; base64 ids are 24 or 12. Anything else is
    malformed and refused rather than decoded into garbage.
    """
    if len(value) in (32, 16) and re.fullmatch(r"[0-9a-fA-F]+", value):
        return value.lower()
    try:
        decoded = base64.b64decode(value, validate=True)
    except ValueError:  # binascii.Error is a ValueError
        # Unpadded base64 (legacy OTLP JSON writers may omit the trailing
        # "="); pad to a multiple of 4 and retry the strict decode.
        try:
            decoded = base64.b64decode(value + "=" * (-len(value) % 4), validate=True)
        except ValueError as exc:  # binascii.Error is a ValueError
            raise ValueError(f"malformed span id {value[:24]!r} (neither hex nor base64)") from exc
    if len(decoded) not in (16, 8):
        raise ValueError(
            f"malformed span id {value[:24]!r}: decoded to {len(decoded)} bytes, "
            "expected 16 (trace) or 8 (span)"
        )
    return decoded.hex()


def _normalize_span(sp: dict) -> dict:
    """One OTLP span dict (proto3 JSON, hex or base64 ids) -> normalized dict."""
    attributes = {a["key"]: _anyvalue(a["value"]) for a in sp.get("attributes", [])}
    kind = sp.get("kind")
    if isinstance(kind, str):
        kind = kind.removeprefix(_KIND_STRIP) or None
    status = sp.get("status", {})
    code = status.get("code", "") if status else ""
    if isinstance(code, str):
        status_out = "ERROR" if code == _STATUS_ERROR else ("OK" if code == _STATUS_OK else None)
    elif isinstance(code, int):
        status_out = "ERROR" if code == 2 else ("OK" if code == 1 else None)
    else:
        status_out = None
    return {
        "span_id": _id_to_hex(sp["spanId"]),
        "parent_span_id": _id_to_hex(sp["parentSpanId"]) if sp.get("parentSpanId") else None,
        "name": sp.get("name", ""),
        "kind": kind,
        "start_ns": int(sp["startTimeUnixNano"]),
        "end_ns": int(sp["endTimeUnixNano"]),
        "status": status_out,
        "attributes": attributes,
    }


def _spans_from_envelope(data: dict, hex_trace_id: str | None) -> list[dict]:
    """Walk an OTLP JSON envelope (Tempo `batches` or `resourceSpans`) for spans."""
    # Tempo full trace: {"batches": [...]}; plain OTLP/JSON: {"resourceSpans": [...]}.
    groups = data.get("resourceSpans") or data.get("batches") or []
    out: list[dict] = []
    for group in groups:
        for ss in group.get("scopeSpans", []):
            for sp in ss.get("spans", []):
                try:
                    if hex_trace_id is not None and _id_to_hex(sp["traceId"]) != hex_trace_id:
                        continue
                    out.append(_normalize_span(sp))
                except (KeyError, ValueError):
                    # A malformed sibling span (empty traceId, missing field,
                    # garbage id) must never abort the whole scan — skip it
                    # and keep going (the pre-#637 behavior).
                    continue
    return out


# ── find mode ────────────────────────────────────────────────────────────────


def _http_get_json(url: str) -> dict:
    import urllib.request

    if not url.startswith(("http://", "https://")):
        raise ValueError(f"refusing non-http(s) URL: {url[:60]!r}")
    # Bypass the system HTTP proxy: cluster endpoints are loopback or on the
    # private cluster network, and the macOS system proxy (Clash/VPN on
    # 127.0.0.1:7897) answers 502 for them.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(url, timeout=60) as resp:
        return json.loads(resp.read().decode())


def cmd_search(args) -> int:
    import urllib.parse

    params = {"q": args.search, "limit": str(args.limit)}
    if args.spss:
        params["spss"] = str(args.spss)
    url = args.tempo_url.rstrip("/") + "/api/search?" + urllib.parse.urlencode(params)
    data = _http_get_json(url)
    traces = data.get("traces", [])
    print(f"{len(traces)} trace(s) match {args.search!r} ({args.tempo_url})")
    for t in traces:
        start_ns = int(t.get("startTimeUnixNano", 0))
        started = datetime.fromtimestamp(start_ns / 1e9, tz=UTC).isoformat()
        # Tempo search trims leading zero nibbles; pad back so the id pastes
        # straight into --trace-id. Lowercase too: the ids are hex, and
        # cmd_fetch accepts [0-9a-f] only.
        trace_id = t["traceID"].lower().zfill(32)
        print(
            f"  {trace_id}  dur={t.get('durationMs')}ms  "
            f"spans={t.get('spanSet', {}).get('matched')}  root={t.get('rootServiceName')}  "
            f"start={started}"
        )
    return 0


# ── fetch mode ────────────────────────────────────────────────────────────────


def fetch_from_tempo(args, hex_trace_id: str) -> list[dict]:
    import urllib.error

    url = f"{args.tempo_url.rstrip('/')}/api/traces/{hex_trace_id}"
    try:
        data = _http_get_json(url)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        if "exceeds max size" in body:
            print(
                "tempo: trace exceeds the 5 MB full-trace cap. "
                "Narrow with --search (spans per span set, a time window)."
            )
            return []
        raise
    spans = _spans_from_envelope(data, hex_trace_id)
    print(f"tempo: {len(spans)} spans from {url}")
    return spans


def cmd_fetch(args) -> int:
    if not args.trace_id:
        print("--trace-id is required in fetch mode")
        return 2
    if not re.fullmatch(r"[0-9a-f]{31,32}", args.trace_id):
        print("--trace-id must be a 31- or 32-char hex id")
        return 2
    trace_id = args.trace_id.zfill(32)
    spans = fetch_from_tempo(args, trace_id)
    if not spans:
        print(
            "no spans found. Is the trace id right? Tempo keeps traces for 7 days, "
            "and a span the collector could not deliver in time is not recorded anywhere else."
        )
        return 1
    spans.sort(key=lambda s: s["start_ns"])
    out = {
        "trace_id": trace_id,
        "source": "tempo",
        "fetched_at": datetime.now(UTC).isoformat(),
        "spans": spans,
    }
    Path(args.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"wrote {len(spans)} spans -> {args.out}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[1])
    ap.add_argument(
        "--tempo-url",
        default=os.environ.get(
            "MEIVA_TRACE_TEMPO_URL",
            os.environ.get("MEIVA_TELEMETRY_TEMPO_QUERY_URL", "http://localhost:3200"),
        ),
        help=(
            "Tempo query API base (or the Grafana datasource proxy path). "
            "Default: MEIVA_TRACE_TEMPO_URL, else MEIVA_TELEMETRY_TEMPO_QUERY_URL "
            "(the per-cluster Tempo query URL; remote when Tempo is not on "
            "this host), else http://localhost:3200."
        ),
    )
    ap.add_argument("--out", default="trace_raw.json")
    mode = ap.add_mutually_exclusive_group(required=True)
    mode.add_argument("--search", metavar="TRACEQL", help="find traces (Tempo search API)")
    mode.add_argument("--trace-id", metavar="HEX32", help="fetch one full trace")
    ap.add_argument("--limit", type=int, default=10, help="search mode: max traces")
    ap.add_argument("--spss", type=int, default=3, help="search mode: spans per span set")
    args = ap.parse_args()
    return cmd_search(args) if args.search else cmd_fetch(args)


if __name__ == "__main__":
    raise SystemExit(main())
