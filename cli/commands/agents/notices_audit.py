# meiva:note
# The audit itself runs on the gateway (`GET /api/notifications/audit`); this module
# only asks for the report and renders it, as a table or as the gateway's JSON.
# meiva:link base/events/notifications/docs/notifications.meiva.okf.md

from __future__ import annotations

import json
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, tzinfo
from typing import Any, cast
from zoneinfo import ZoneInfo

import httpx

_TIMEOUT_S = 60.0


def _table(headers: Sequence[object], rows: Sequence[Sequence[object]]) -> list[str]:
    cells = [[str(cell) for cell in row] for row in rows]
    titles = [str(header) for header in headers]
    widths = [len(title) for title in titles]
    for row in cells:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = ["  ".join(title.ljust(widths[i]) for i, title in enumerate(titles)).rstrip()]
    lines.append("  ".join("-" * width for width in widths))
    for row in cells:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return lines


def _bar(value: int, peak: int, width: int = 40) -> str:
    if value == 0 or peak == 0:
        return ""
    return "#" * max(1, round(value * width / peak))


def _per_day_section(report: Mapping[str, Any]) -> list[str]:
    categories = cast(list[str], report["categories"])
    rows: list[list[object]] = [["day", *categories, "total"]]
    totals: Counter[str] = Counter()
    for row in cast(list[dict[str, Any]], report["delivered"]["per_day"]):
        counts = cast(dict[str, int], row["by_category"])
        totals.update(counts)
        rows.append([row["day"], *(counts[name] for name in categories), row["total"]])
    rows.append(["total", *(totals[name] for name in categories), sum(totals.values())])
    return ["", "Deliveries per day", *_table(rows[0], rows[1:])]


def _per_category_section(report: Mapping[str, Any]) -> list[str]:
    delivered = cast(dict[str, int], report["delivered"]["per_category"])
    grand_total = sum(delivered.values()) or 1
    return [
        "",
        "Per category",
        *_table(
            ["category", "delivered", "share"],
            [[name, count, f"{count / grand_total:.1%}"] for name, count in delivered.items()],
        ),
    ]


def _per_hour_section(report: Mapping[str, Any]) -> list[str]:
    hours = cast(list[int], report["delivered"]["per_hour"])
    peak = max(hours) if hours else 0
    return [
        "",
        f"Per hour ({report['window']['timezone']})",
        *_table(
            ["hour", "count", ""],
            [[f"{hour:02d}", count, _bar(count, peak)] for hour, count in enumerate(hours)],
        ),
    ]


def _candidates_section(report: Mapping[str, Any]) -> list[str]:
    rows = cast(list[dict[str, Any]], report["candidates"])
    return [
        "",
        "Candidates and suppression",
        *_table(
            ["category", "candidates", "delivered", "suppressed", "repeat deliveries"],
            [
                [
                    row["category"],
                    row["candidates"],
                    row["delivered"],
                    row["suppressed"],
                    row["repeat_deliveries"],
                ]
                for row in rows
            ],
        ),
    ]


def _simulation_section(report: Mapping[str, Any]) -> list[str]:
    rows = cast(list[dict[str, Any]], report["simulations"])
    return [
        "",
        "Simulations (one flipped input at a time, against the configured baseline)",
        *_table(
            ["variant", "delivered", "delta", "per day"],
            [
                [row["variant"], row["delivered"], f"{row['delta']:+d}", f"{row['per_day']:.1f}"]
                for row in rows
            ],
        ),
    ]


def _explain_section(report: Mapping[str, Any]) -> list[str]:
    rows = cast(list[dict[str, Any]], report.get("explain") or [])
    if not rows:
        return []
    stamp = "%Y-%m-%d %H:%M"
    return [
        "",
        "Why each candidate was or was not delivered",
        *_table(
            ["at", "category", "agent", "outcome", "dedupe key"],
            [
                [
                    datetime.fromisoformat(row["at"]).strftime(stamp),
                    row["category"],
                    row["agent_id"],
                    row["outcome"],
                    row["dedupe_key"] or "-",
                ]
                for row in rows
            ],
        ),
    ]


def render_text(report: Mapping[str, Any], *, tz: tzinfo) -> list[str]:
    window = cast(dict[str, Any], report["window"])
    stamp = "%Y-%m-%d %H:%M"
    start = datetime.fromisoformat(cast(str, window["start"])).astimezone(tz)
    end = datetime.fromisoformat(cast(str, window["end"])).astimezone(tz)
    return [
        f"Notification audit — {window['days']} day{'' if window['days'] == 1 else 's'} "
        f"ending {end:{stamp}} {window['timezone']}",
        f"window    {start:{stamp}} .. {end:{stamp}}",
        *_source_lines(cast(dict[str, str], report["sources"])),
        "switches  "
        + "  ".join(
            f"{name}={_on_off(value=value)}"
            for name, value in cast(dict[str, bool], report["switches"]).items()
        ),
        *_per_day_section(report),
        *_per_category_section(report),
        *_per_hour_section(report),
        *_candidates_section(report),
        *_simulation_section(report),
        *_explain_section(report),
    ]


def _source_lines(sources: Mapping[str, str]) -> list[str]:
    entries = list(sources.items())
    width = max(len(name) for name in sources)
    head = f"sources   {entries[0][0].ljust(width)}  {entries[0][1]}"
    return [head, *(f"          {name.ljust(width)}  {origin}" for name, origin in entries[1:])]


def render_json(report: Mapping[str, Any]) -> str:
    return json.dumps(report, indent=2, sort_keys=False)


def _on_off(*, value: bool) -> str:
    return "on" if value else "off"


_Get = Callable[..., httpx.Response]


# meiva:note
# The gateway's API base, its auth headers and the cluster-internal GET.
def _gateway_dial() -> tuple[str, dict[str, str], _Get]:
    from base.cluster.machine import gateway_api_base, gateway_auth_headers
    from base.cluster.machine.ambient import gateway_access_from_settings
    from base.host.net.http_dial import get as dial_get

    access = gateway_access_from_settings()  # ambient: C
    return gateway_api_base(access.gateway_url), gateway_auth_headers(access), dial_get


# meiva:note
# The gateway's audit report, or (None, why) when the gateway rejected the request.
def _audit_report(
    params: dict[str, Any], dial: Callable[[], tuple[str, dict[str, str], _Get]]
) -> tuple[dict[str, Any] | None, str | None]:
    base, headers, get = dial()
    resp = get(
        f"{base}/api/notifications/audit", timeout=_TIMEOUT_S, headers=headers, params=params
    )
    if resp.status_code == 422:
        detail = resp.json().get("detail")
        return None, detail if isinstance(detail, str) else json.dumps(detail)
    resp.raise_for_status()
    return resp.json(), None


def cmd_notices_audit(
    *,
    days: int,
    category: str | None,
    tz_name: str | None,
    now: datetime | None,
    simulate: Sequence[str],
    out: str = "text",
    explain: bool = False,
    dial: Callable[[], tuple[str, dict[str, str], _Get]] = _gateway_dial,
) -> int:
    params: dict[str, Any] = {"days": days, "explain": explain, "simulate": list(simulate)}
    if category is not None:
        params["category"] = category
    if tz_name is not None:
        params["tz"] = tz_name
    if now is not None:
        params["now"] = now.isoformat()
    report, rejected = _audit_report(params, dial)
    if report is None:
        print(f"meiva: {rejected}", file=sys.stderr)
        return 2
    if out == "json":
        print(render_json(report))
    else:
        zone = ZoneInfo(report["window"]["timezone"])
        print("\n".join(render_text(report, tz=zone)))
    return 0
