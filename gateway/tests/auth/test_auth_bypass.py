# meiva:note
# The cluster auth middleware is the only thing standing between an anonymous caller and
# every gateway route, and its bypass table is the only list of routes it serves without a
# credential — but nothing checks that the two still describe each other. A renamed or
# dropped route leaves an entry that admits nothing, a path registered later inherits an
# exemption nobody granted, and the alert webhook (Grafana's only way into the alert store)
# is admitted by one entry in that table and by no other test.
#
# So these tests reconcile the table against the routes the app actually registered, then
# prove both directions over real HTTP: the ingest endpoint reaches its handler with no
# cookie and no Authorization, and a neighbouring route still 401s under the same client.
# The proof is the response and the stored row the handler itself produced, never a
# constant that says the path is exempt.

from __future__ import annotations

from collections.abc import Iterable, Iterator
from typing import Any, cast

import psycopg
from fastapi import FastAPI

from gateway.app import cluster_auth_middleware
from gateway.tests.auth.gateway_config import anonymous_gateway_client, gateway_test_app

# meiva:note
# The methods a browser or a webhook actually calls. The route expansion also yields the HEAD
# and OPTIONS forms a router synthesises and the mounts it carries; no bypass entry may name
# either, so they stay out of the registered set.
_REQUEST_METHODS = frozenset({"GET", "POST", "PUT", "PATCH", "DELETE"})

# meiva:note
# A minimal Alertmanager standard webhook body: one firing instance, which is what Grafana's
# co-located Alertmanager contact point posts. It must be valid, because an invalid body is
# rejected at the request boundary and would prove only that validation runs.
_ALERT_WEBHOOK = {
    "status": "firing",
    "alerts": [
        {
            "status": "firing",
            "labels": {"alertname": "auth-bypass-gate", "severity": "error"},
            "annotations": {"summary": "anonymous ingest proof"},
            "startsAt": "2026-11-03T00:00:00Z",
            "endsAt": "",
            "fingerprint": "auth-bypass-gate",
            "generatorURL": "http://localhost:3002/alerting/proof/edit",
        }
    ],
}


# meiva:note
# The two bypass tables, read from the namespace the middleware was defined in. The table belongs
# with the code that consults it, not with the app that mounts it; resolving the owner from the
# function keeps this reconciliation pointed at its reader, so moving the middleware moves the
# evidence with it instead of leaving a hard-coded import behind.
def _bypass_tables() -> tuple[frozenset[str], frozenset[tuple[str, str]]]:
    namespace = cluster_auth_middleware.__globals__
    return (
        cast("frozenset[str]", namespace["_AUTH_BYPASS_PATHS"]),
        cast("frozenset[tuple[str, str]]", namespace["_AUTH_BYPASS_METHOD_PATHS"]),
    )


# meiva:note
# Every (method, path) the app registered, plus the distinct paths among them. This FastAPI
# keeps one lazy `_IncludedRouter` per `include_router` call and computes the included paths only
# when it matches a request, so `app.routes` must be walked through `effective_candidates()`;
# the gateway route-contract test does the same walk for the same reason.
# meiva:link gateway/tests/bootstrap/test_route_contracts_gateway.py
def _registered_routes(app: FastAPI) -> tuple[set[tuple[str, str]], set[str]]:
    pairs: set[tuple[str, str]] = set()
    for route in _effective_routes(app.routes):
        original = getattr(route, "original_route", route)
        path = getattr(route, "path", None) or getattr(original, "path", None)
        if not path:
            continue
        for method in getattr(original, "methods", None) or ():
            if method in _REQUEST_METHODS:
                pairs.add((method, path))
    return pairs, {path for _method, path in pairs}


def _effective_routes(routes: Iterable[Any]) -> Iterator[Any]:
    for route in routes:
        candidates = getattr(route, "effective_candidates", None)
        if candidates is None:
            yield route
        else:
            yield from _effective_routes(candidates())


# meiva:note
# The reconciliation. Each entry of either table must name a route the app registered: a
# path-only entry needs that path under some method, a method entry needs that exact pair.
# Collecting every stale entry before failing reports the whole drift at once, which is what
# the reader of a red build needs.
def test_auth_bypass_table_matches_registered_routes() -> None:
    paths, method_paths = _bypass_tables()
    pairs, registered_paths = _registered_routes(gateway_test_app())

    assert paths or method_paths, (
        "the auth bypass table is empty, so reconciling it against the registered routes "
        "proves nothing: the middleware would reject every anonymous request"
    )

    stale = [f"{path} (any method)" for path in sorted(paths) if path not in registered_paths]
    stale += [
        f"{method} {path}" for method, path in sorted(method_paths) if (method, path) not in pairs
    ]
    assert stale == [], (
        "the auth bypass table must reconcile with the registered routes: "
        f"{'; '.join(stale)} not registered. Every entry in "
        f"{cluster_auth_middleware.__module__}._AUTH_BYPASS_PATHS / "
        "_AUTH_BYPASS_METHOD_PATHS grants anonymous admission, so it must name a route the "
        "app actually serves — drop the entry with the route, or re-add the route."
    )


# meiva:note
# The forward direction, over real HTTP and with no credential at all: a Grafana-shaped POST
# reaches the handler, the handler stores the instance, and the 200 body is the handler's own
# ingest result. Remove the POST /api/alerts entry and the middleware answers 401 before the
# body is read, so this test fails; the assertion below that the request carried no
# Authorization and no cookie keeps the 200 from being explained by a credential the test
# client injected.
def test_alert_ingest_reaches_handler_without_any_credential(
    db_conn: psycopg.Connection,
) -> None:
    with anonymous_gateway_client(
        fields={"webhook_token": None, "im_notify_enabled": False},
        client=("127.0.0.1", 5000),
    ) as client:
        resp = client.post("/api/alerts", json=_ALERT_WEBHOOK)

    assert "authorization" not in resp.request.headers
    assert "cookie" not in resp.request.headers
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"processed": 1, "inserted": 1, "updated": 0, "notified": 0}

    with db_conn.cursor() as cur:
        cur.execute(
            "SELECT fingerprint FROM alerts WHERE fingerprint = %s",
            ("auth-bypass-gate",),
        )
        assert cur.fetchall() == [("auth-bypass-gate",)]


# meiva:note
# The reverse control, under the same client and the same anonymous condition: the bypass is
# scoped to one method of one path, so GET on that very path and a POST somewhere else are
# still refused — by the middleware, whose typed envelope names the reason. Without this a
# bug that admitted everything would read as the forward test passing.
def test_protected_routes_still_refuse_the_same_anonymous_client() -> None:
    with anonymous_gateway_client(
        fields={"webhook_token": None, "im_notify_enabled": False},
        client=("127.0.0.1", 5000),
    ) as client:
        same_path_other_method = client.get("/api/alerts")
        another_protected_post = client.post("/api/agents", json={})

    for resp in (same_path_other_method, another_protected_post):
        assert resp.status_code == 401, resp.text
        assert resp.json()["code"] == "authentication_required"
