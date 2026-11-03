"""F12/F12b sampling helpers: probe rounds, tccd window parsing, attribution joins."""

from __future__ import annotations

import datetime
import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import TypedDict

from base.clock import Timers
from scripts.boundary.untyped import is_object_dict, object_of

from .support import (
    JsonObject,
    SmokeError,
    fail,
    json_object,
    json_objects,
    pid_alive,
    run,
    save,
    wait_for,
    whole_number,
)

_F12_ROUND_WAIT_S = 10.0

_F12_REQUESTING_RE = re.compile(
    r"requesting=\{TCCDProcess: identifier=(?P<requesting_id>[^,]*), pid=(?P<requesting_pid>\d+)"
)
_F12_RESPONSIBLE_RE = re.compile(
    r"responsible=\{TCCDProcess: identifier=(?P<responsible_id>[^,]*), pid=(?P<responsible_pid>\d+)"
)
_LOG_STAMP_RE = re.compile(r"^(?P<stamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}\.\d{3})")


class JoinedRequest(TypedDict):
    # meiva:note
    # One tccd request line joined to a probe round: when it was logged and the responsible process it names (None when the line names none).

    line_ts: float
    responsible_id: str | None
    responsible_pid: int | None


class JoinedRow(TypedDict):
    # meiva:note
    # One probe round with the first tccd requests its pid made at or after the round.

    unit: str
    point: str
    pid: int
    requests: list[JoinedRequest]


class PointCounts(TypedDict):
    requests: int
    attributed: int
    unattributed: int
    missing_rounds: int


class AttributionSummary(TypedDict):
    totals: PointCounts
    by_point: dict[str, PointCounts]


def _f12_chain_line(pid: int) -> str:
    proc = run(["ps", "-o", "pid=,ppid=,pgid=,lstart=,command=", "-p", str(pid)], check=False)
    return proc.stdout.strip() or f"{pid} <gone>"


def _f12_round(
    workdir: Path, unit_id: str, tag: str, pid: int, timeout: float, *, timers: Timers
) -> JsonObject:
    rounds_path = workdir / f"unit-results-{unit_id}.json.rounds"

    def _found() -> JsonObject | None:
        if not rounds_path.exists():
            return None
        for line in reversed(rounds_path.read_text().splitlines()):
            try:
                record = object_of(json.loads, line)
            except json.JSONDecodeError:
                continue
            if is_object_dict(record) and record.get("round") == tag and record.get("pid") == pid:
                return record
        return None

    return wait_for(
        f"round {tag} from unit {unit_id} pid {pid}", _found, timeout, "f12", timers=timers
    )


def f12_point(
    workdir: Path,
    tag: str,
    units: list[tuple[str, int]],
    pids: list[int],
    *,
    now: float,
    timers: Timers,
) -> JsonObject:
    """Trigger one probe round per unit id; collect per-pid replies; snapshot chains.

    A unit that is already dead gets an `error` marker without waiting; a live
    unit that never answers times out into `error: no probe response`. The join
    skips error rows; the summary counts them as missing rounds.
    """
    smoke_ts = round(now, 3)
    for unit_id in sorted({unit_id for unit_id, _ in units}):
        (workdir / f"unit-results-{unit_id}.json.req").write_text(tag)
    rows: list[JsonObject] = []
    for unit_id, pid in units:
        if not pid_alive(pid):
            record: JsonObject = {"round": tag, "pid": pid, "error": "unit dead"}
        else:
            try:
                record = _f12_round(workdir, unit_id, tag, pid, _F12_ROUND_WAIT_S, timers=timers)
            except SmokeError:
                record = {"round": tag, "pid": pid, "error": "no probe response"}
        record["unit"] = unit_id
        rows.append(record)
    return {"smoke_ts": smoke_ts, "rows": rows, "chains": [_f12_chain_line(pid) for pid in pids]}


def f12_parse_window(log_text: str) -> dict[int, list[tuple[float, str]]]:
    """Index AUTHREQ_ATTRIBUTION lines by requesting pid: pid -> [(ts, line)]."""
    lines: dict[int, list[tuple[float, str]]] = {}
    for line in log_text.splitlines():
        stamp = _LOG_STAMP_RE.match(line)
        requesting = _F12_REQUESTING_RE.search(line)
        if stamp is None or requesting is None:
            continue
        ts = (
            datetime.datetime.strptime(stamp.group("stamp"), "%Y-%m-%d %H:%M:%S.%f")
            .astimezone()
            .timestamp()
        )
        lines.setdefault(int(requesting.group("requesting_pid")), []).append((ts, line))
    return lines


# meiva:note
# Every probe row of every sampling point.
def _probe_rows(samples: Mapping[str, object]) -> list[JsonObject]:
    points = json_object(samples["points"], "samples points")
    return [
        row
        for point in points.values()
        for row in json_objects(json_object(point, "samples point")["rows"], "point rows")
    ]


def _number(record: Mapping[str, object], key: str) -> float:
    value = record[key]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise TypeError(f"{key} is {type(value).__name__}, not a number")
    return float(value)


# meiva:note
# The `ts` of a row that carries a probe reply, else None (an error marker has none).
def _answered_at(record: Mapping[str, object]) -> float | None:
    ts = record.get("ts")
    return float(ts) if isinstance(ts, (int, float)) else None


def f12_join(
    samples: Mapping[str, object], lines: dict[int, list[tuple[float, str]]]
) -> list[JoinedRow]:
    """Join probe rounds against the parsed window: first 3 requests at/after ts - 0.15s."""
    joined: list[JoinedRow] = []
    for record in _probe_rows(samples):
        answered_at = _answered_at(record)
        if answered_at is None:
            continue
        requests: list[JoinedRequest] = []
        for ts, line in lines.get(whole_number(record, "pid"), []):
            if ts < answered_at - 0.15:
                continue
            responsible = _F12_RESPONSIBLE_RE.search(line)
            requests.append(
                JoinedRequest(
                    line_ts=ts,
                    responsible_id=responsible.group("responsible_id") if responsible else None,
                    responsible_pid=int(responsible.group("responsible_pid"))
                    if responsible
                    else None,
                )
            )
            if len(requests) >= 3:
                break
        joined.append(
            JoinedRow(
                unit=str(record["unit"]),
                point=str(record["round"]),
                pid=whole_number(record, "pid"),
                requests=requests,
            )
        )
    return joined


def _joined_rows(samples: Mapping[str, object]) -> list[JsonObject]:
    return json_objects(samples["tccd"], "samples tccd")


def f12_attribution_summary(samples: Mapping[str, object]) -> AttributionSummary:
    """Aggregate sampled rounds: per-point + total requests/unattributed/missing."""
    by_point: dict[str, PointCounts] = {}

    def counts_of(point: str) -> PointCounts:
        return by_point.setdefault(
            point, PointCounts(requests=0, attributed=0, unattributed=0, missing_rounds=0)
        )

    for record in _probe_rows(samples):
        counts = counts_of(str(record["round"]))
        if _answered_at(record) is None:
            counts["missing_rounds"] += 1
    for row in _joined_rows(samples):
        counts = counts_of(str(row["point"]))
        for request in json_objects(row["requests"], "joined requests"):
            counts["requests"] += 1
            if request["responsible_pid"] is None:
                counts["unattributed"] += 1
            else:
                counts["attributed"] += 1
    totals = PointCounts(
        requests=sum(counts["requests"] for counts in by_point.values()),
        attributed=sum(counts["attributed"] for counts in by_point.values()),
        unattributed=sum(counts["unattributed"] for counts in by_point.values()),
        missing_rounds=sum(counts["missing_rounds"] for counts in by_point.values()),
    )
    return AttributionSummary(totals=totals, by_point=by_point)


def f12_expect_attribution(
    samples: Mapping[str, object], expectations: list[tuple[str, list[int] | None, int]]
) -> list[str]:
    """Check sampled requests resolve to the expected responsible pid.

    Each expectation is (point, pids, want_pid): at `point`, every request of
    every joined row whose pid is listed (None = all joined rows) must carry a
    responsible pid equal to `want_pid`, and each listed pid must have a joined
    row. Points absent from `expectations` are pure observations. Returns one
    human-readable violation string per failed check.
    """
    rows_by_point: dict[str, list[JsonObject]] = {}
    for row in _joined_rows(samples):
        rows_by_point.setdefault(str(row["point"]), []).append(row)
    violations: list[str] = []
    for point, pids, want_pid in expectations:
        joined = rows_by_point.get(point, [])
        if pids is None:
            selected = joined
            if not selected:
                violations.append(f"{point}: no sampled rows")
                continue
        else:
            wanted = set(pids)
            selected = [row for row in joined if row["pid"] in wanted]
            seen = {row["pid"] for row in selected}
            for pid in sorted(wanted - seen):
                violations.append(f"{point}: no sampled row for pid {pid} (dead or no response)")
        for row in selected:
            if not row["requests"]:
                violations.append(f"{point} {row['unit']} pid {row['pid']}: no requests observed")
                continue
            for request in json_objects(row["requests"], "joined requests"):
                if request["responsible_pid"] is None:
                    violations.append(
                        f"{point} {row['unit']} pid {row['pid']}: request has no responsible"
                    )
                elif request["responsible_pid"] != want_pid:
                    violations.append(
                        f"{point} {row['unit']} pid {row['pid']}: responsible pid "
                        f"{request['responsible_pid']} != expected {want_pid}"
                    )
    return violations


def _f12_summary_text(label: str, samples: Mapping[str, object]) -> str:
    """One log line with the aggregate request counts for a sampling phase."""
    totals = json_object(json_object(samples["summary"], "samples summary")["totals"], "totals")
    return (
        f"{label}: {totals['requests']} requests, {totals['unattributed']} unattributed, "
        f"{totals['missing_rounds']} missing rounds"
    )


def f12_finish(
    evidence: Path,
    samples: JsonObject,
    label: str,
    expectations: list[tuple[str, list[int] | None, int]],
    *,
    now: float,
) -> None:
    """Join the tccd window, save the sampling record, fail on attribution violations."""
    _f12_join_tccd(evidence, samples, now=now, name=f"{label}-tccd-window.txt")
    print(_f12_summary_text(label, samples))
    violations = f12_expect_attribution(samples, expectations)
    if violations:
        samples["violations"] = violations
    save(evidence, f"{label}-sampling.json", json.dumps(samples, indent=2))
    if violations:
        fail(label, f"attribution violations: {'; '.join(violations)}")


def _f12_join_tccd(
    evidence: Path,
    samples: JsonObject,
    *,
    now: float,
    name: str = "f12-tccd-window.txt",
) -> None:
    """Join sampled probe rounds against the tccd AUTHREQ_ATTRIBUTION log window.

    The only IO on the F12b attribution path: pulls the log window, saves it as
    `name` under `evidence`, then joins + summarizes (pure helpers above) into
    `samples`.
    """
    points = json_object(samples["points"], "samples points")
    first = min(
        _number(json_object(point, "samples point"), "smoke_ts") for point in points.values()
    )
    span = max(2, int((now - first) / 60) + 2)
    log_text = run(
        [
            "/usr/bin/log",
            "show",
            "--last",
            f"{span}m",
            "--style",
            "compact",
            "--predicate",
            'eventMessage CONTAINS "AUTHREQ_ATTRIBUTION"',
        ],
        timeout=120.0,
    ).stdout
    save(evidence, name, log_text)
    samples["tccd"] = f12_join(samples, f12_parse_window(log_text))
    samples["summary"] = f12_attribution_summary(samples)
