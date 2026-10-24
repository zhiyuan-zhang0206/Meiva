# meiva:note
# The machine verbs of `meiva cluster`, end to end against a real listener.
#
# The gateway is a scripted HTTP server on a loopback port that the verb dials through its
# real transport and real headers (`gateway_server`); what the verb sent is read from what
# the server received, and what it printed from the captured streams. The wording of every
# verdict is decided from values and asserted in `test_gateway_replies.py` and
# `test_roster_table.py`; this file proves the shell dials the right endpoint with the right
# credential, maps the gateway's answer to an exit status and prints what the plan says.

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime

import httpx
import pytest

from base.api_contracts.status import MachineStatus
from base.cluster.machine import GatewayApiBaseMissing, GatewayApiTokenMissing
from base.host.net.tests.local_http import closed_port_url
from cli.commands.cluster.control import (
    cmd_cluster_mark_staging,
    cmd_cluster_pause,
    cmd_cluster_resume,
    cmd_cluster_status,
    fetch_gateway_cluster_status,
)
from cli.commands.cluster.tests.gateway_server import SECRET, Reply, access_to, serving
from cli.commands.cluster.tests.roster_rows import machine

_BEARER = f"Bearer {SECRET}"
_ROSTER = ("GET", "/api/cluster/roster")


def _wire(*machines: MachineStatus) -> list[dict[str, object]]:
    return [m.model_dump(mode="json") for m in machines]


def test_status_dials_the_fresh_roster_with_the_gateway_credential_and_renders_it(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rows = _wire(
        machine(name="cloud", head_sha="abc1234def", running_sha="abc1234def"),
        machine(name="stale", serve_gateway=False, head_sha="abc1234def", running_sha="999888777"),
        machine(name="impostor", serve_gateway=False, online=False, identity_mismatch=True),
    )
    with serving({_ROSTER: Reply(body=rows)}) as gateway:
        rc = cmd_cluster_status(access=access_to(gateway.url), status_probe_timeout_s=5.0)

    assert rc == 0
    (seen,) = gateway.requests
    assert seen.target == "/api/cluster/roster?fresh=true"
    assert seen.headers["authorization"] == _BEARER
    out = capsys.readouterr().out
    assert "MISMATCH" in out and "⚠ 9998887" in out
    assert out.splitlines()[0].split()[:2] == ["name", "role"]


def test_status_of_an_empty_machines_table_says_no_host_has_started(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({_ROSTER: Reply(body=[])}) as gateway:
        rc = cmd_cluster_status(access=access_to(gateway.url), status_probe_timeout_s=5.0)

    assert rc == 0
    assert "machines table empty" in capsys.readouterr().out


def test_status_names_the_http_status_a_gateway_answers_with(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({_ROSTER: Reply(status=503, body={"detail": "starting"})}) as gateway:
        rc = cmd_cluster_status(access=access_to(gateway.url), status_probe_timeout_s=5.0)

    assert rc == 1
    err = capsys.readouterr().err
    assert "gateway returned HTTP 503" in err
    assert f"{gateway.url}/api/cluster/roster?fresh=true" in err


def test_status_reports_a_refused_connection_instead_of_raising(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cmd_cluster_status(access=access_to(closed_port_url()), status_probe_timeout_s=5.0)

    assert rc == 1
    assert "✗ gateway unreachable at http://127.0.0.1:" in capsys.readouterr().err


def test_status_reports_a_silent_gateway_after_the_derived_budget(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # meiva:note
    # The budget is the gateway's per-machine probe bound plus the margin, so a black-holed
    # machine's probe can finish before the client gives up (#4900): 0.2s + 0.1s here.
    with serving({_ROSTER: Reply(body=[], delay_s=2.0)}) as gateway:
        rc = cmd_cluster_status(
            access=access_to(gateway.url), status_probe_timeout_s=0.2, read_margin_s=0.1
        )

    assert rc == 1
    err = capsys.readouterr().err
    assert "did not respond within 0.3s" in err
    assert "adds 0.1s of slack" in err


def test_status_of_a_host_with_no_gateway_url_says_why(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = cmd_cluster_status(access=access_to(""), status_probe_timeout_s=5.0)

    assert rc == 1
    assert "✗ cannot resolve gateway URL: gateway_url unset" in capsys.readouterr().err


def test_marking_staging_posts_the_flag_and_reports_the_new_state(
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = ("POST", "/api/cluster/machines/runner-a/staging")
    with serving({route: Reply(body={"is_staging": True})}) as gateway:
        marked = cmd_cluster_mark_staging(
            "runner-a", is_staging=True, access=access_to(gateway.url)
        )
        unmarked = cmd_cluster_mark_staging(
            "runner-a", is_staging=False, access=access_to(gateway.url)
        )

    assert (marked, unmarked) == (0, 0)
    assert [seen.body for seen in gateway.requests] == [{"is_staging": True}, {"is_staging": False}]
    assert all(seen.headers["authorization"] == _BEARER for seen in gateway.requests)
    assert capsys.readouterr().out == (
        "runner-a: marked staging\nrunner-a: unmarked staging (now a fan-out target)\n"
    )


def test_a_machine_the_registry_does_not_know_exits_1_for_every_verb(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with serving({}) as gateway:
        access = access_to(gateway.url)
        results = (
            cmd_cluster_mark_staging("ghost", is_staging=True, access=access),
            cmd_cluster_pause("ghost", reason=None, access=access),
            cmd_cluster_resume("ghost", access=access),
        )

    assert results == (1, 1, 1)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "no machine named 'ghost' in the registry\n" * 3


def test_pausing_posts_the_reason_and_prints_what_the_gateway_did(
    capsys: pytest.CaptureFixture[str],
) -> None:
    reply = {
        "paused_at": datetime(2026, 9, 27, tzinfo=UTC).isoformat(),
        "pause_reason": "disk swap",
        "terminated_agents": 3,
        "force_marked_agents": 1,
        "reassigned_tasks": 4,
    }
    with serving({("POST", "/api/cluster/machines/runner-a/pause"): Reply(body=reply)}) as gateway:
        rc = cmd_cluster_pause("runner-a", reason="disk swap", access=access_to(gateway.url))

    assert rc == 0
    (seen,) = gateway.requests
    assert seen.body == {"reason": "disk swap"}
    out = capsys.readouterr().out
    assert "runner-a: PAUSED (since 2026-09-27T00:00:00) — disk swap" in out
    assert "terminated via the machine's ops server: 3" in out
    assert "tasks drained to #405: 4" in out


def test_pausing_without_a_reason_sends_an_empty_one(capsys: pytest.CaptureFixture[str]) -> None:
    reply = {
        "paused_at": "2026-09-27T00:00:00+00:00",
        "pause_reason": "",
        "terminated_agents": 0,
        "force_marked_agents": 0,
        "reassigned_tasks": 0,
    }
    with serving({("POST", "/api/cluster/machines/runner-a/pause"): Reply(body=reply)}) as gateway:
        assert cmd_cluster_pause("runner-a", reason=None, access=access_to(gateway.url)) == 0

    assert gateway.requests[0].body == {"reason": ""}
    assert "rollout" not in capsys.readouterr().out


def test_a_pause_the_gateway_refuses_prints_its_reason_and_exits_1(
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = ("POST", "/api/cluster/machines/gw/pause")
    with serving(
        {route: Reply(status=400, body={"detail": "cannot pause the gateway itself"})}
    ) as g:
        rc = cmd_cluster_pause("gw", reason=None, access=access_to(g.url))

    assert rc == 1
    captured = capsys.readouterr()
    assert captured.err == "cannot pause the gateway itself\n" and captured.out == ""


def test_resuming_prints_the_verdict_and_the_machine_side_checklist(
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = ("POST", "/api/cluster/machines/runner-a/resume")
    with serving({route: Reply(body={"name": "runner-a", "resumed": True})}) as gateway:
        rc = cmd_cluster_resume("runner-a", access=access_to(gateway.url))

    assert rc == 0
    assert gateway.requests[0].body == {}
    out = capsys.readouterr().out
    assert out.startswith("runner-a: resumed — probing / roster / fan-out / spawn restored\n")
    assert "machine-side checklist (run ON runner-a when it is back online)" in out


def test_a_gateway_error_other_than_not_found_raises_rather_than_masking(
    capsys: pytest.CaptureFixture[str],
) -> None:
    route = ("POST", "/api/cluster/machines/runner-a/resume")
    with (
        serving({route: Reply(status=500, body={})}) as gateway,
        pytest.raises(httpx.HTTPStatusError),
    ):
        cmd_cluster_resume("runner-a", access=access_to(gateway.url))

    assert capsys.readouterr().out == ""


def test_the_cluster_status_fetch_presents_the_gateways_credential() -> None:
    body = {"machine_name": "gw", "serve_gateway": True, "serve_agent_runner": False}
    with serving({("GET", "/api/cluster/status"): Reply(body=body)}) as gateway:
        fetched = fetch_gateway_cluster_status(access_to(gateway.url))

    assert fetched == body
    (seen,) = gateway.requests
    assert seen.target == "/api/cluster/status" and seen.headers["authorization"] == _BEARER


def test_the_cluster_status_fetch_without_a_credential_raises_instead_of_dialing() -> None:
    # meiva:note
    # The API always authenticates: with neither a token nor a secret the call is named, not
    # sent as a blank bearer. The credential precedence itself is base.cluster.machine's
    # contract.
    # meiva:link base/cluster/machine/docs/machine.meiva.okf.md
    with serving({}) as gateway:
        credentialless = replace(access_to(gateway.url), cluster_secret="")
        with pytest.raises(GatewayApiTokenMissing):
            fetch_gateway_cluster_status(credentialless)

    assert gateway.requests == []


def test_the_cluster_status_fetch_of_a_host_with_no_gateway_url_raises_the_named_error() -> None:
    with pytest.raises(GatewayApiBaseMissing):
        fetch_gateway_cluster_status(access_to(""))
