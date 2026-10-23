"""`base.daemon.health` probe paths: probe_daemon/probe_home fresh-vs-stale verdicts,
commit/identity checks; split from base/daemon/tests/test_health.py (task #4922)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from pathlib import Path

import pytest

from base.config import ConfigBoot, get_field
from base.daemon import health
from base.daemon.health_schema import DEGRADED, component
from base.daemon.tests._health_helpers import find_free_port, http_get, probe_url
from base.daemon.tests.health_support import unknown_image
from base.daemon.tests.probe_resources import commit_source, json_response, wire_response
from base.host.env.health_ports import health_port
from base.native_process import loaded_commit
from base.paths import meiva_home

# ─── probe_daemon: a 200 is believed only from a verified identity ───────────
#
# Regression cover for the 2026-07-24 outage: a pytest-leaked daemon fell back
# to prod's default health port and answered 200 for 98 minutes while prod's
# own daemon was dead. Every probe read green, so the watchdog never
# respawned. These run a REAL health server on a free port and vary exactly one
# element of the identity at a time.


async def _probe(name: str, port: int, pidfile: Path) -> health.DaemonProbe:
    """Run the (blocking) probe off the event loop.

    `probe_daemon` is sync by design — its callers are cron-invoked healthchecks
    and the watchdog, which already runs each check via `asyncio.to_thread`.
    Calling it inline here would block the same loop that serves the health
    server under test, and every probe would "time out" against a live daemon."""
    return await asyncio.to_thread(
        health.probe_daemon,
        name,
        probe_url(port),
        pidfile=pidfile,
    )


@pytest.mark.asyncio
async def test_healthz_body_carries_home(tmp_path: Path) -> None:
    """`home` is in the payload at all — the field the cross-cluster check reads."""
    port = find_free_port()
    server = await health.start_health_server("agent_host", port=port, image=unknown_image())
    try:
        _status, body = await http_get(port, "/healthz")
        assert json.loads(body)["home"] == str(meiva_home())
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_body_carries_the_daemons_own_commit(tmp_path: Path) -> None:
    sha = commit_source(tmp_path, "loaded image")
    image = loaded_commit.LoadedCommit.capture(tmp_path)
    port = find_free_port()
    server = await health.start_health_server("agent_host", port=port, image=image)
    try:
        _status, body = await http_get(port, "/healthz")
        assert json.loads(body)["sha"] == sha
        assert commit_source(tmp_path, "checkout advanced") != sha
        status, body = await http_get(port, "/healthz")
        assert status == 200
        assert json.loads(body)["sha"] == sha
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_healthz_reports_an_unfrozen_process_as_unknown() -> None:
    """A daemon that froze no commit says so rather than omitting the key — an
    absent field reads to a probe as an old daemon that predates this payload,
    a null reads as "this process cannot vouch for its code"."""
    image = unknown_image()
    port = find_free_port()
    server = await health.start_health_server("agent_host", port=port, image=image)
    try:
        _status, body = await http_get(port, "/healthz")
        assert json.loads(body)["sha"] is None
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
@pytest.mark.parametrize("sha", ["c0ffee1234", None])
async def test_healthz_retains_explicit_loaded_image_after_source_changes(
    tmp_path: Path, sha: str | None
) -> None:
    """A bound server reports its captured image, including an honest unknown."""

    sha = commit_source(tmp_path, "captured image") if sha is not None else None
    image = loaded_commit.LoadedCommit.capture(tmp_path)
    assert commit_source(tmp_path, "replacement image") != sha
    port = find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    server = await health.start_health_server("agent_host", port=port, image=image)
    try:
        for _ in range(2):
            status, body = await http_get(port, "/healthz")
            payload = json.loads(body)
            assert status == 200
            assert payload["sha"] == sha
            assert payload["readiness"] == "ok"
        probe = await _probe("agent_host", port, pidfile)
        assert probe.alive is True
        assert probe.detail == (
            f"pid {os.getpid()}, code {sha[:7]}" if sha is not None else f"pid {os.getpid()}"
        )
        assert image.source_root == tmp_path
        assert image.sha == sha
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_reports_the_commit_without_judging_it(tmp_path: Path) -> None:
    """The commit rides in the detail, never in the verdict.

    A daemon on stale code is alive. Failing the probe on a commit mismatch
    would have every watchdog respawn its daemon the moment a rollout advances
    the checkout, racing the orchestrated restart it is supposed to leave alone."""
    image = loaded_commit.LoadedCommit(Path.cwd(), "c0ffee1234")
    port = find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    server = await health.start_health_server("agent_host", port=port, image=image)
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.alive is True, probe.detail
        assert "c0ffee1" in probe.detail
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_alive_when_name_home_and_pid_all_match(tmp_path: Path) -> None:
    """The happy path: our own daemon, its own pidfile → alive."""
    port = find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    server = await health.start_health_server("agent_host", port=port, image=unknown_image())
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.alive is True, probe.detail
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_200_from_a_process_that_is_not_ours(tmp_path: Path) -> None:
    """THE outage: something answers a healthy 200 on our port, but the pid is not
    the one our pidfile recorded → dead, so the watchdog respawns instead of
    idling on a stranger's green light.

    Verdict DOWN, not terminal: name and home already matched, so the stray belongs
    to THIS cluster and this daemon kind — `respawn_service` kills our own
    `meiva-agent-host` session first, which does free the port."""
    port = find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid() + 1))  # our daemon's pid, not the responder's
    server = await health.start_health_server("agent_host", port=port, image=unknown_image())
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.DOWN
        assert probe.terminal is False
        assert "pid" in probe.detail
    finally:
        await health.stop_health_server(server)


# meiva:note
# #5525's family A: a process that is not this unit's daemon answers the port, the probe can
# only say "not this unit's daemon", and three restarts recount the same anonymous blocker.
# The detail must name the listener, and an unavailable lookup must stay a verdict rather
# than become a probe failure.
@pytest.mark.asyncio
async def test_a_missing_pidfile_names_the_process_listening_on_the_port(tmp_path: Path) -> None:
    port = find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    server = await health.start_health_server("agent_host", port=port, image=unknown_image())
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.DOWN
        assert "answers but no pidfile" in probe.detail
        assert f"listening: pid {os.getpid()}" in probe.detail
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_an_unavailable_listener_lookup_leaves_the_detail_usable(tmp_path: Path) -> None:
    port = find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    server = await health.start_health_server("agent_host", port=port, image=unknown_image())

    def no_discovery(_url: str) -> str:
        return "unknown"

    try:
        probe = await asyncio.to_thread(
            health.probe_daemon_identity,
            "agent_host",
            probe_url(port),
            pidfile=pidfile,
            listener_lookup=no_discovery,
        )
        assert probe.verdict is health.ProbeVerdict.DOWN
        assert "not this unit's daemon; listening: unknown" in probe.detail
    finally:
        await health.stop_health_server(server)


def test_probe_rejects_daemon_from_another_home(tmp_path: Path) -> None:
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    payload = {"name": "agent_host", "pid": os.getpid(), "home": str(tmp_path / "other-home")}
    with wire_response(json_response(payload)) as url:
        probe = health.probe_daemon("agent_host", url, pidfile=pidfile)
    assert probe.verdict is health.ProbeVerdict.PORT_TAKEN
    assert probe.terminal is True
    assert "home=" in probe.detail
    assert "another unit's daemon holds this port" in probe.detail


@pytest.mark.asyncio
async def test_probe_rejects_a_different_daemon_kind(tmp_path: Path) -> None:
    """A labeler squatting on the agent host's port is not a live agent host — and
    terminal, because killing `meiva-agent-host` does not free a port `meiva-labeler`
    holds."""
    port = find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    server = await health.start_health_server("labeler", port=port, image=unknown_image())
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.PORT_TAKEN
        assert "name=" in probe.detail
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_200_when_no_pidfile_exists(tmp_path: Path) -> None:
    """No pidfile + something answering = an impostor, NOT "unverifiable, assume
    alive". Daemons write their pidfile before binding /healthz, so our own
    daemon can never be in this state.

    DOWN rather than terminal: name and home matched first, so the answerer is a
    stray of this same cluster, which the respawn's kill-session clears."""
    port = find_free_port()
    server = await health.start_health_server("agent_host", port=port, image=unknown_image())
    try:
        probe = await _probe("agent_host", port, tmp_path / "absent.pid")
        assert probe.verdict is health.ProbeVerdict.DOWN
        assert "pidfile" in probe.detail
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_rejects_non_json_responder(tmp_path: Path) -> None:
    """An unrelated HTTP server on the port (no JSON identity) → dead, and
    terminal: it is not an Meiva daemon at all, so nothing this unit supervises can
    be restarted to take the port back."""

    async def _handle(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await _reader.read(1024)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nConnection: close\r\n\r\nhello")
        await writer.drain()
        writer.close()

    port = find_free_port()
    server = await asyncio.start_server(_handle, host="127.0.0.1", port=port)
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.PORT_TAKEN
        assert "not JSON" in probe.detail
    finally:
        await health.stop_health_server(server)


def test_probe_dead_when_nothing_listens(tmp_path: Path) -> None:
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    probe = health.probe_daemon(
        "agent_host", probe_url(find_free_port()), pidfile=pidfile, timeout_s=1.0
    )
    assert probe.verdict is health.ProbeVerdict.DOWN
    assert probe.terminal is False, "a free port is the respawnable case"
    assert "unreachable" in probe.detail


@pytest.mark.asyncio
async def test_probe_dead_when_liveness_is_stale(tmp_path: Path) -> None:
    """A wedged main loop flips /healthz to 503; identity is irrelevant then.

    DOWN, so the respawn still runs — a wedged loop in our own daemon is exactly
    what a respawn cures."""
    port = find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    stale = health.Liveness(timeout_s=-1.0)
    server = await health.start_health_server(
        "agent_host", port=port, liveness=stale, image=unknown_image()
    )
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.DOWN
    finally:
        await health.stop_health_server(server)


@pytest.mark.asyncio
async def test_probe_includes_degraded_component_reasons(tmp_path: Path) -> None:
    """A watchdog failure names the stuck component rather than an opaque 503."""
    port = find_free_port()
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    server = await health.start_health_server(
        "agent_host",
        port=port,
        components=[component("ops", DEGRADED, detail="update-lock held 7200s")],
        image=unknown_image(),
    )
    try:
        probe = await _probe("agent_host", port, pidfile)
        assert probe.verdict is health.ProbeVerdict.DOWN
        assert probe.detail == "healthz returned HTTP 503; degraded: ops: update-lock held 7200s"
    finally:
        await health.stop_health_server(server)


def test_health_port_env_override() -> None:
    config = ConfigBoot()
    config.read_process_environment()
    config.view.services.agent_host_health_port = 9999
    assert health_port("agent_host", read_field=config.get_field) == 9999


def test_health_port_unknown_raises_key_error() -> None:
    """Unregistered daemon name — fail fast (no silent fallback)."""
    with pytest.raises(KeyError):
        health_port("never_registered_daemon", read_field=get_field)


# ─── probe_daemon always returns a verdict ───────────────────────────────
#
# The watchdog isolates each check, so a raising probe never took a round down —
# it just meant the service was never judged alive-or-dead, so NO RESTART was
# ever attempted, while every 60s round wrote a fresh multi-KB traceback. Six
# healthchecks route through probe_daemon (heartbeat, labeler, memory-indexer,
# ops, events-maintenance, agent_host), so one escaping exception type silences
# six services' revival at once.


def test_probe_daemon_survives_an_http_exception(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    pidfile = tmp_path / "agent_host.pid"
    pidfile.write_text(str(os.getpid()))
    with (
        wire_response(b"garbage on the wire\r\n") as url,
        caplog.at_level(logging.ERROR, logger="base.daemon.health"),
    ):
        probe = health.probe_daemon("agent_host", url, pidfile=pidfile)
    assert probe.alive is False
    assert "BadStatusLine" in probe.detail
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)


def test_probe_daemon_survives_a_pidfile_oserror(tmp_path: Path) -> None:
    pidfile = tmp_path / "x.pid"
    pidfile.mkdir()
    payload = {"name": "agent_host", "home": str(meiva_home()), "pid": os.getpid()}
    with wire_response(json_response(payload)) as url:
        probe = health.probe_daemon("agent_host", url, pidfile=pidfile)
    assert probe.alive is False
    assert "IsADirectoryError" in probe.detail


def test_probe_daemon_verdict_is_down_never_up_on_failure(tmp_path: Path) -> None:
    with wire_response(b"garbage on the wire\r\n") as url:
        probe = health.probe_daemon("labeler", url, pidfile=tmp_path / "x.pid")
    assert probe.verdict is health.ProbeVerdict.DOWN
    assert probe.terminal is False


def test_probe_daemon_passes_through_a_normal_verdict(tmp_path: Path) -> None:
    pidfile = tmp_path / "x.pid"
    pidfile.write_text("42")
    payload = {"name": "ops", "home": str(meiva_home()), "pid": 42}
    with wire_response(json_response(payload)) as url:
        probe = health.probe_daemon("ops", url, pidfile=pidfile)
    assert probe.alive is True
    assert probe.detail == "pid 42"


# ─── probe_home: identity without a pid ──────────────────────────────────
#
# The gateway serves `/api/health`, which `probe_home` checks on `home` alone —
# no `pid`, because uvicorn's reload fork means a healthy gateway routinely
# answers with a pid its own pidfile never recorded, and not yet `name`, which
# the payload now carries but no probe may read until it has rolled out
# fleet-wide (#1038). `probe_home` is that weaker check, shared by the gateway
# healthcheck and (through `ServiceSpec.identity_probe`) by `meiva status` and
# `meiva cluster health-probe`, so the watchdog and the operator cannot be told
# different things about the same port.


def test_probe_home_alive_when_home_matches() -> None:
    with wire_response(json_response({"status": "ok", "home": str(meiva_home())})) as url:
        probe = health.probe_home(url)
    assert probe.verdict is health.ProbeVerdict.ALIVE


def test_probe_home_alive_on_a_payload_carrying_name() -> None:
    payload = {"status": "ok", "name": "gateway", "home": str(meiva_home())}
    with wire_response(json_response(payload)) as url:
        probe = health.probe_home(url)
    assert probe.verdict is health.ProbeVerdict.ALIVE


def test_probe_home_rejects_another_units_home() -> None:
    with wire_response(json_response({"home": "/home/meiva/.meiva"})) as url:
        probe = health.probe_home(url)
    assert probe.verdict is health.ProbeVerdict.PORT_TAKEN
    assert "/home/meiva/.meiva" in probe.detail
    assert "another unit's daemon holds this port" in probe.detail


def test_probe_home_rejects_a_body_with_no_identity() -> None:
    with wire_response(json_response({"status": "ok"})) as url:
        assert health.probe_home(url).alive is False


def test_probe_home_unreachable_is_down_not_terminal() -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        probe = health.probe_home(probe_url(listener.getsockname()[1]))
    assert probe.verdict is health.ProbeVerdict.DOWN
    assert probe.terminal is False


def test_probe_home_always_returns_a_verdict() -> None:
    with wire_response(b"garbage on the wire\r\n") as url:
        probe = health.probe_home(url)
    assert probe.verdict is health.ProbeVerdict.DOWN
    assert "BadStatusLine" in probe.detail


def test_probe_fails_closed_on_any_unexpected_exception() -> None:
    with wire_response(b"garbage on the wire\r\n") as url:
        assert health.probe_home(url).alive is False


def test_probe_passes_through_a_normal_verdict() -> None:
    with wire_response(json_response({"home": str(meiva_home())})) as url:
        probe = health.probe_home(url)
    assert probe.alive is True
    assert probe.detail == f"home {meiva_home()}"
