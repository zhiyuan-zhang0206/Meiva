# meiva:note
# The destinations the rendered Grafana tree names are reachable on real listeners.
#
# The rendered `MEIVA_PG_URL` is dialed with libpq against the throwaway Postgres the fixtures
# provision, and the rendered alert webhook is POSTed to a listener on port 0 standing in for the
# gateway. Which URL a configuration yields is decided in `plans/lgtm/urls.py` and tested there;
# this case proves the rendered values are dialable, with and without a remote observatory.

from __future__ import annotations

from dataclasses import replace
from urllib.parse import urlsplit

import httpx
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from cli.commands.observability.plans.observability_config import AlertWebhookInputs
from cli.commands.observability.tests.lgtm.lgtm_harness import LgtmUnit, lgtm_unit
from cli.commands.observability.tests.lgtm.lgtm_harness import (
    operator_pipeline as operator_pipeline,
)
from cli.commands.observability.tests.observability_inputs import lgtm_settings, observability
from cli.commands.observability.tests.scripted_http import Reply, serve
from cli.database.tests.command_inputs import command_runtime as command_runtime

__all__ = ["lgtm_unit"]


@pytest.mark.parametrize("observatory", ["", "http://observatory.test"])
def test_rendered_endpoints_reach_private_postgres_and_the_gateway_listener(
    db_url: str, lgtm_unit: LgtmUnit, observatory: str
) -> None:
    pg = conninfo_to_dict(db_url)
    with serve({"/api/alerts": Reply(status=204)}) as gateway:
        port = urlsplit(gateway.url).port
        assert port is not None
        settings = replace(
            lgtm_settings(observability=observability(observability_url=observatory)),
            webhook=AlertWebhookInputs(gateway_url=gateway.url, gateway_port=port),
        )

        lgtm_unit.render(settings=settings, direct_db_url=db_url)
        env = lgtm_unit.runtime_env()

        # meiva:note
        # Identity is asserted before dialing: even a regressed renderer must never send this
        # private test to the operator's default ports.
        assert env["MEIVA_PG_URL"] == f"127.0.0.1:{pg['port']}"
        target = urlsplit("postgresql://" + str(env["MEIVA_PG_URL"]))
        with psycopg.connect(db_url, host=target.hostname, port=target.port) as conn:
            assert conn.execute("SELECT 42").fetchone() == (42,)
        webhook = str(env["MEIVA_ALERTS_WEBHOOK_URL"])
        assert (
            webhook == f"{gateway.url}/api/alerts"
            if observatory
            else f"http://127.0.0.1:{port}/api/alerts"
        )
        with httpx.Client(trust_env=False, timeout=3) as client:
            assert client.post(webhook).status_code == 204

    assert [request.path for request in gateway.received] == ["/api/alerts"]
