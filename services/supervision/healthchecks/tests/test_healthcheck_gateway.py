# meiva:note
# `services.supervision.healthchecks.gateway` unit tests — the `gateway_probe` branches.
#
# A 2xx is necessary but NOT sufficient: the body must also report this unit's
# `$MEIVA_HOME`, otherwise the responder is some other cluster's gateway (or an
# unrelated process) sitting on the port and the real gateway is dead. Identity is
# home-only here, deliberately without the pid comparison the daemon healthchecks
# apply — uvicorn reload serves from a worker forked out of the process that wrote
# `gateway_pidfile`, so a healthy gateway routinely answers with an unrecorded pid.
#
# The check itself is `base.daemon.health.probe_home`, shared with the operator
# surfaces (`meiva status` / `meiva cluster health-probe` reach it through
# `ServiceSpec.identity_probe`) so the watchdog and the human cannot be told
# different things about the same port. These tests exercise it through the
# healthcheck, which is the caller whose behaviour they pin.
#
# The gateway is a real HTTP listener on port 0 that answers with a scripted status and
# body.

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from base.daemon.health import ProbeVerdict
from base.paths import meiva_home
from services.supervision.healthchecks import gateway as hc
from services.supervision.healthchecks.tests.http_stub import HttpStub


def _own_health_body() -> bytes:
    return json.dumps({"status": "ok", "home": str(meiva_home()), "machine": "m"}).encode()


# meiva:note
# 200 + this unit's home -> alive.
def test_probe_alive_when_home_matches() -> None:
    with HttpStub(body=_own_health_body()) as gateway:
        assert hc.gateway_probe_at(gateway.url, home=meiva_home()).alive is True


# meiva:note
# 200 from a gateway whose `$MEIVA_HOME` is not ours -> dead, not "healthy".
#
# The foreign-unit impostor case: another unit's gateway (or a leaked test one)
# holding this port would otherwise read as green forever while this unit's
# gateway stays down. The verdict is TERMINAL — this unit cannot kill the gateway
# session on another unit's socket (the socket lives under `$MEIVA_HOME`), so
# respawning into the bound port every 60s is a loop, not a repair.
def test_probe_rejects_gateway_from_another_home() -> None:
    other = json.dumps(
        {"status": "ok", "home": str(Path.home() / ".meiva-some-other-cluster")}
    ).encode()
    with HttpStub(body=other) as gateway:
        probe = hc.gateway_probe_at(gateway.url, home=meiva_home())

    assert probe.verdict is ProbeVerdict.PORT_TAKEN
    assert probe.terminal is True
    assert "home=" in probe.detail


def test_probe_rejects_body_without_home() -> None:
    """A 200 whose body carries no identity at all is not evidence of our gateway."""
    with HttpStub(body=b'{"status": "ok"}') as gateway:
        assert hc.gateway_probe_at(gateway.url, home=meiva_home()).alive is False


# meiva:note
# Some unrelated HTTP server on the port -> dead.
def test_probe_rejects_non_json_body() -> None:
    with HttpStub(body=b"<html>nginx</html>") as gateway:
        probe = hc.gateway_probe_at(gateway.url, home=meiva_home())

    assert probe.verdict is ProbeVerdict.PORT_TAKEN
    assert "not JSON" in probe.detail


def test_probe_dead_on_http_500() -> None:
    with HttpStub(status=500) as gateway:
        assert hc.gateway_probe_at(gateway.url, home=meiva_home()).alive is False


# meiva:note
# Connection refused -> dead.
def test_probe_dead_on_connection_refused() -> None:
    with HttpStub() as gateway:
        url = gateway.url

    assert hc.gateway_probe_at(url, home=meiva_home()).alive is False


# meiva:note
# A reply that is not HTTP (`http.client.BadStatusLine`) is not an `OSError`, so the inner
# probe's narrow catch misses it. Losing the verdict matters most here: the gateway is
# what the cluster health probe polls, with `--auto-rollback --threshold 3` armed, and a
# probe that raises means no restart is ever attempted while every 60s round writes a
# fresh traceback.
def test_probe_survives_a_malformed_response(caplog: pytest.LogCaptureFixture) -> None:
    with (
        HttpStub(raw=b"not an http status line\r\n\r\n") as gateway,
        caplog.at_level(logging.ERROR, logger="base.daemon.health"),
    ):
        probe = hc.gateway_probe_at(gateway.url, home=meiva_home())

    assert probe.alive is False
    assert "BadStatusLine" in probe.detail
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)
