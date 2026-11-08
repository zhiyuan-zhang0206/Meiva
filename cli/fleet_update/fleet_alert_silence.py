# meiva:note
# The deploy window's alert silence: one Grafana silence over every rule, opened by
# `meiva update down` and shortened to a post-`up` settle grace by `up`.
#
# An update stops the whole cluster on purpose, so every rule that watches a service would fire
# for a planned outage. Notification policy owns merging and repetition; this is the one place a
# planned window is expressed: a silence in the co-located Grafana's Alertmanager whose matcher
# covers every alert but the disk ones (`alertname=~".+"`, `metric!="host_disk"`,
# `attributes_check!="disk_usage"`), carrying an expiry and a comment, created before the
# first stop and shortened to `SETTLE_WINDOW` (10 minutes) after `up` succeeds. A silenced rule
# still evaluates and enters `Firing` during the `down` window, and Grafana rules hold firing
# instances through `keepFiringFor: 5m` / `missing_series_evals_to_resolve: 6` (plus the 5-minute
# cron health probe and resume claim tail) before resolving; keeping the silence for `SETTLE_WINDOW`
# after `up` lets outage-induced firings resolve quietly while any condition that truly outlives
# the settle window notifies as soon as the window ends.
#
# `cli.fleet_update` pipes this file's source to `python -` on the gateway host (the same way it
# ships its roster program), so the first update that carries this change already has it and the
# program needs nothing from the host's checkout but `base.config`. Opening is idempotent (a rerun
# of `down` extends the silence it already owns instead of stacking another) and every failure is
# reported, never raised: a window without a silence is noisy, not unsafe, so it must not strand
# an update.
#
#     python - open --hours 4 --comment "..."   # prints `SILENCE opened ...`
#     python - close                            # prints `SILENCE closed N`
# meiva:link cli/docs/fleet-update-window/fleet-update-window.meiva.okf.md

from __future__ import annotations

import argparse
import base64
import json
import sys
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, timedelta
from functools import partial
from http.client import HTTPResponse
from typing import TypeGuard

CREATED_BY = "meiva-fleet-update"
SILENCES = "/api/alertmanager/grafana/api/v2/silences"
SETTLE_WINDOW = timedelta(minutes=10)
# meiva:note
# Every alert but a full disk: the disk fills the same during an update, so the host filesystem rules
# (`metric=host_disk`) and the health probe's disk check must still page.
_MATCH_EVERY_ALERT = [
    {"name": "alertname", "value": ".+", "isRegex": True, "isEqual": True},
    {"name": "metric", "value": "host_disk", "isRegex": False, "isEqual": False},
    {"name": "attributes_check", "value": "disk_usage", "isRegex": False, "isEqual": False},
]
_TIMEOUT_S = 15.0
_LIVE_STATES = ("active", "pending")


# meiva:note
# Grafana could not be reached or refused the call.
class GrafanaError(RuntimeError):
    pass


def _is_object_dict(value: object) -> TypeGuard[dict[str, object]]:
    return isinstance(value, dict)


def _is_object_list(value: object) -> TypeGuard[list[object]]:
    return isinstance(value, list)


# meiva:note
# `function(*args)` as an `object`, so a call the standard library types as `Any` is read as
# `object`. This file is piped to the gateway host as source, so it carries its own copy of
# `base.host.boundary.untyped.object_of`.
def _object_of[**P](function: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    return function(*args, **kwargs)


# meiva:note
# The body of the answer `opener(request)` gives. `urlopen` types its answer `Any`; a parameter
# typed with the real `HTTPResponse` takes it without naming the `Any`.
def _read_body(
    opener: Callable[[urllib.request.Request], AbstractContextManager[HTTPResponse]],
    request: urllib.request.Request,
) -> bytes:
    with opener(request) as response:
        return response.read()


# meiva:note
# Call the co-located Grafana's Alertmanager API with its Basic credential and decode the JSON body.
def _call(base: str, credential: str, method: str, path: str, body: object = None) -> object:
    token = base64.b64encode(f"admin:{credential}".encode()).decode()
    headers = {"Authorization": f"Basic {token}"}
    data = None if body is None else json.dumps(body).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(base + path, data=data, method=method, headers=headers)  # noqa: S310
    try:
        raw = _read_body(partial(urllib.request.urlopen, timeout=_TIMEOUT_S), request)
    except urllib.error.HTTPError as error:
        raise GrafanaError(f"{method} {path}: HTTP {error.code}") from error
    except (OSError, urllib.error.URLError) as error:
        raise GrafanaError(f"{method} {path}: {type(error).__name__}") from error
    return _object_of(json.loads, raw) if raw else None


# meiva:note
# Every live silence this program created.
def _owned(base: str, credential: str) -> list[dict[str, object]]:
    rows = _call(base, credential, "GET", SILENCES)
    items = rows if _is_object_list(rows) else []
    owned: list[dict[str, object]] = []
    for row in items:
        if not _is_object_dict(row) or row.get("createdBy") != CREATED_BY:
            continue
        status = row.get("status")
        if _is_object_dict(status) and status.get("state") in _LIVE_STATES:
            owned.append(row)
    return owned


# meiva:note
# Create (or extend the one already owned) silence over every alert; its id.
def open_window(base: str, credential: str, *, hours: float, comment: str, now: datetime) -> str:
    ends_at = (now + timedelta(hours=hours)).astimezone(UTC).isoformat(timespec="seconds")
    silence: dict[str, object] = {
        "matchers": _MATCH_EVERY_ALERT,
        "startsAt": now.astimezone(UTC).isoformat(timespec="seconds"),
        "endsAt": ends_at,
        "createdBy": CREATED_BY,
        "comment": comment,
    }
    owned = _owned(base, credential)
    if owned:
        silence["id"] = owned[0]["id"]
        silence["startsAt"] = owned[0]["startsAt"]
    created = _call(base, credential, "POST", SILENCES, silence)
    if not _is_object_dict(created):
        raise GrafanaError(f"POST {SILENCES}: unexpected response payload")
    return str(created["silenceID"])


# meiva:note
# Shorten every live silence this program created to the post-`up` settle window (`now + SETTLE_WINDOW`)
# so rules that entered `Firing` during `down` can complete their 5m `keepFiringFor` / 6-evaluation
# recovery window and 5m health-probe cron tick before Alertmanager unsilences them; the ids updated.
def close_window(base: str, credential: str, *, now: datetime) -> list[str]:
    ends_at = (now + SETTLE_WINDOW).astimezone(UTC).isoformat(timespec="seconds")
    closed: list[str] = []
    for row in _owned(base, credential):
        updated = _call(
            base,
            credential,
            "POST",
            SILENCES,
            {
                "id": row["id"],
                "matchers": row.get("matchers", _MATCH_EVERY_ALERT),
                "startsAt": row["startsAt"],
                "endsAt": ends_at,
                "createdBy": CREATED_BY,
                "comment": row.get("comment", ""),
            },
        )
        if not _is_object_dict(updated):
            raise GrafanaError(f"POST {SILENCES}: unexpected response payload")
        closed.append(str(updated["silenceID"]))
    return closed


# meiva:note
# The co-located Grafana's base URL and admin credential; None when this home has no credential.
def colocated_grafana() -> tuple[str, str] | None:
    from base.config import settings

    secret = settings.alerts.grafana_admin_password
    if secret is None or not secret.get_secret_value():
        return None
    return settings.observability.telemetry_grafana_url.rstrip("/"), secret.get_secret_value()


# meiva:note
# The options of `main`, as the typed namespace argparse fills in (`open` carries hours and comment).
class _Arguments(argparse.Namespace):
    verb: str
    hours: float
    comment: str


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="fleet_alert_silence")
    verbs = parser.add_subparsers(dest="verb", required=True)
    opened = verbs.add_parser("open")
    opened.add_argument("--hours", type=float, required=True)
    opened.add_argument("--comment", required=True)
    verbs.add_parser("close")
    args = parser.parse_args(argv, namespace=_Arguments())
    grafana = colocated_grafana()
    if grafana is None:
        print("SILENCE skipped: no Grafana admin credential is configured on this host")
        return 0
    base, credential = grafana
    try:
        now = datetime.now(UTC)
        if args.verb == "open":
            silence_id = open_window(
                base, credential, hours=args.hours, comment=args.comment, now=now
            )
            until = (now + timedelta(hours=args.hours)).isoformat(timespec="minutes")
            print(f"SILENCE opened id={silence_id} until={until}")
        else:
            print(f"SILENCE closed {len(close_window(base, credential, now=now))}")
    except GrafanaError as error:
        print(f"SILENCE failed: {error}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
