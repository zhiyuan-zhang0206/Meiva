"""`services.desktop.browser.probe` — the one service whose identity CDP cannot state.

Every other Meiva service proves it is ours from its own `/healthz` body. Chrome's
DevTools Protocol carries no field we control, so identity is assembled from two
facts this host can observe: a Chrome running on THIS cluster's `--user-data-dir`
(`orphan.find_cluster_chrome`, the positive profile-token identification) that is
also holding the LISTEN socket on the CDP port being dialled.

Both halves are load-bearing and the tests pin each one separately:

- without the profile half, another unit's browser relayed onto this host's
  loopback reads as ours;
- without the listening half, our Chrome merely *existing* would vouch for a port
  it never won — which is exactly what happens when the relay binds first and
  Chrome comes up with a dead DevTools endpoint.

No Chrome is started: the CDP dial and the process facts are stubbed, so the
verdicts are asserted directly.
"""

from __future__ import annotations

import logging
import socket
import threading
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, NoReturn

import pytest

import services.desktop.browser.probe as probe_mod
from base.daemon.health import DaemonProbe, ProbeVerdict

_PROFILE = Path("/home/u/.meiva/chrome-profile")


# meiva:note
# Evaluate `decide_browser_probe` from CDP reachability, the cluster's Chrome pids,
# who holds the port, and the holder's own `(name, argv)`.
def _decide(
    *,
    cdp: str | None,
    chromes: list[int],
    listening: dict[int, bool | None],
    holder: int | None = None,
    holder_facts: tuple[str, list[str] | None] | None = None,
    answered: bool = False,
    wait_reason: str | None = None,
) -> DaemonProbe:
    return probe_mod.decide_browser_probe(
        9222,
        _PROFILE,
        answer=probe_mod.CdpAnswer(cdp, answered=answered if cdp is not None else True),
        found=chromes,
        listening=listening,
        holder=holder,
        holder_facts=("", None) if holder_facts is None else holder_facts,
        wait_reason=wait_reason,
    )


@contextmanager
def _serve_cdp_once(status: int, body: bytes) -> Generator[int]:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        def _handle() -> None:
            try:
                conn, _addr = server.accept()
            except OSError:
                return
            with conn:
                try:
                    conn.recv(4096)
                    head = (
                        f"HTTP/1.1 {status} Stat\r\n"
                        f"Content-Length: {len(body)}\r\n"
                        "Connection: close\r\n\r\n"
                    ).encode("ascii")
                    conn.sendall(head + body)
                except OSError:
                    return

        thread = threading.Thread(target=_handle, daemon=True)
        thread.start()
        try:
            yield port
        finally:
            thread.join(timeout=2.0)


def _closed_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def test_alive_when_our_chrome_holds_the_port() -> None:
    """CDP answers and the listener is a Chrome on this cluster's profile."""
    verdict = _decide(cdp=None, chromes=[42], listening={42: True})
    assert verdict.verdict is ProbeVerdict.ALIVE
    assert "42" in verdict.detail


def test_cdp_unreachable_is_down_not_terminal() -> None:
    """Nothing serving = the respawnable case. `respawn_service` kills the stale
    session and relaunches, which is the fix — so this must NOT be terminal."""
    verdict = _decide(cdp="CDP unreachable: ConnectionRefusedError", chromes=[], listening={})
    assert verdict.verdict is ProbeVerdict.DOWN
    assert verdict.terminal is False


def test_cdp_unreachable_names_a_deliberate_macos_readiness_wait() -> None:
    verdict = _decide(
        cdp="CDP unreachable: ConnectionRefusedError",
        chromes=[],
        listening={},
        wait_reason="login Keychain is not ready",
    )
    assert verdict.verdict is ProbeVerdict.DOWN
    assert "waiting" in verdict.detail
    assert "Keychain" in verdict.detail


def test_foreign_chrome_on_the_port_is_terminal() -> None:
    """A debuggable Chrome answers but none of ours is running: another unit's
    browser (or a hand-started one) holds the port. Terminal — the daemon refuses
    to launch while the port is served, so respawning is a 60s crash loop."""
    verdict = _decide(cdp=None, chromes=[], listening={})
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert verdict.terminal is True
    assert str(_PROFILE) in verdict.detail


def test_our_chrome_that_lost_the_bind_does_not_vouch() -> None:
    """The half a profile check alone cannot do. Our Chrome is alive on our
    profile, but the CDP answer comes from something else — the relay won the
    bind and Chrome is running with a dead DevTools endpoint. Existing is not
    owning, so this is still PORT_TAKEN."""
    verdict = _decide(cdp=None, chromes=[42], listening={42: False})
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN


def test_our_chrome_with_a_dead_endpoint_names_its_pids() -> None:
    """The operator-facing half of the win 2026-08-11 shape: the detail must say
    our Chrome is RUNNING (pids) but lost the port, not read like "no Chrome
    exists" — the two point at entirely different remedies."""
    verdict = _decide(cdp=None, chromes=[42], listening={42: False})
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert "running ([42])" in verdict.detail
    assert "DevTools endpoint is dead" in verdict.detail


def test_unreadable_sockets_are_reported_not_assumed() -> None:
    """ "Could not look" is not "not the listener", and it is certainly not
    "healthy". Fail closed, and say which pid could not be read."""
    verdict = _decide(cdp=None, chromes=[42], listening={42: None})
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert "could not be read" in verdict.detail
    assert "42" in verdict.detail


def test_a_listening_chrome_wins_over_an_unreadable_one() -> None:
    """One unreadable sibling must not mask a positive identification."""
    verdict = _decide(cdp=None, chromes=[41, 42], listening={41: None, 42: True})
    assert verdict.verdict is ProbeVerdict.ALIVE


def _raise_for_traceback(exc: BaseException) -> NoReturn:
    raise exc


def test_probe_always_returns_a_verdict(caplog: pytest.LogCaptureFixture) -> None:
    """The probe contract: an unforeseen failure is reported down with a logged
    traceback, never raised at the watchdog and never called alive."""
    with caplog.at_level(logging.ERROR, logger="services.desktop.browser.probe"):
        try:
            _raise_for_traceback(RuntimeError("nobody predicted this"))
        except RuntimeError as exc:
            verdict = probe_mod.classify_probe_exception(exc)
    assert verdict.verdict is ProbeVerdict.DOWN
    assert "RuntimeError" in verdict.detail
    assert any("raised unexpectedly" in r.getMessage() for r in caplog.records)


def test_an_inspection_failure_is_unavailable_not_down(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The 2026-10-13 shape at the probe boundary: the process scan raising the
    psutil C-extension's SystemError is "we could not look", not "the browser is
    down". UNAVAILABLE is terminal — reported, never respawned — so one
    process's mid-scan race cannot swap the Chrome generation and lose every
    agent's pages; the next ordinary round simply re-observes."""
    with caplog.at_level(logging.ERROR, logger="services.desktop.browser.probe"):
        try:
            _raise_for_traceback(
                SystemError(
                    "<built-in function proc_cmdline> returned a result with an exception set"
                )
            )
        except SystemError as exc:
            verdict = probe_mod.classify_probe_exception(exc)
    assert verdict.verdict is ProbeVerdict.UNAVAILABLE
    assert verdict.terminal is True
    assert "SystemError" in verdict.detail
    assert any("inspection failed" in r.getMessage() for r in caplog.records)


def test_the_holder_and_socket_reads_survive_the_psutil_race() -> None:
    """`process_facts` is `_readable_facts` one pid at a time and `listens_on`
    reads the same process: the race must come back as "cannot tell" —
    (("", None)) and None — never as an exception into the probe."""
    race = SystemError("<built-in function proc_cmdline> returned a result with an exception set")

    class _RacingProc:
        def name(self) -> str:
            return ""

        def cmdline(self) -> list[str]:
            raise race

        def net_connections(self, kind: str) -> list[object]:
            raise race

    racing: Any = _RacingProc()
    assert probe_mod.read_process_facts(racing) == ("", None)
    assert probe_mod.read_listens_on(racing, 9222) is None
    assert probe_mod.process_facts(999_999_999) == ("", None)
    assert probe_mod.listens_on(999_999_999, 9222) is None


def test_the_listener_is_ours_even_when_the_walk_missed_it() -> None:
    """The false-terminal class the win 2026-08-11 diagnosis chased: when the
    profile walk misses the Chrome that actually serves CDP (unreadable argv,
    a failed socket read), the probe must not conclude "another unit's browser".
    The listener-first direction reads the holder's own argv, so a missed walk
    is not a false foreign verdict."""
    verdict = _decide(
        cdp=None,
        chromes=[],
        listening={},
        holder=28408,
        holder_facts=("chrome.exe", [f"--user-data-dir={_PROFILE}", "--no-first-run"]),
    )
    assert verdict.verdict is ProbeVerdict.ALIVE
    assert "28408" in verdict.detail


def test_the_holder_who_is_not_ours_names_its_pid() -> None:
    """A positively foreign listener gets named in the detail — the pid is the
    actionable half for an operator hunting the occupant."""
    verdict = _decide(
        cdp=None,
        chromes=[],
        listening={},
        holder=99,
        holder_facts=("firefox.exe", ["--profile", "/elsewhere"]),
    )
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert "pid 99" in verdict.detail


def test_an_unidentifiable_holder_stays_terminal_without_guessing() -> None:
    """The holder's argv cannot be read and it is not in the walk — terminal,
    never guessed at as alive."""
    verdict = _decide(cdp=None, chromes=[], listening={}, holder=99)
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN


def test_an_unreadable_global_table_falls_back_to_the_walk() -> None:
    """The listener-first direction is a gain, not a dependency: when the global
    table cannot be read, the profile-walk + per-pid socket check still decides."""
    verdict = _decide(cdp=None, chromes=[42], listening={42: True}, holder=None)
    assert verdict.verdict is ProbeVerdict.ALIVE


def test_cdp_200_with_empty_body_is_unreachable() -> None:
    """The 2026-09-09 swap-pressure shape: HTTP 200 with an EMPTY body. A
    status-only probe called that alive; the body check must call it dead —
    and record that something answered, so identity (not the status) decides
    the verdict."""
    with _serve_cdp_once(200, b"") as port:
        answer = probe_mod.cdp_unreachable(port)
    assert answer.reason is not None
    assert answer.answered is True


def test_cdp_200_with_non_json_body_is_unreachable() -> None:
    with _serve_cdp_once(200, b"<html>not json</html>") as port:
        answer = probe_mod.cdp_unreachable(port)
    assert answer.reason is not None
    assert answer.answered is True


def test_cdp_200_with_json_missing_browser_is_unreachable() -> None:
    with _serve_cdp_once(200, b'{"WebKit-Version": "537.36"}') as port:
        answer = probe_mod.cdp_unreachable(port)
    assert answer.reason is not None
    assert answer.answered is True


def test_an_http_error_status_counts_as_answered() -> None:
    """urlopen raises HTTPError on 4xx/5xx; the status is still an HTTP answer,
    so it takes the identity path (an occupant holds the port) rather than the
    nothing-answered respawnable DOWN (#2692's exact churn class)."""
    with _serve_cdp_once(502, b"") as port:
        answer = probe_mod.cdp_unreachable(port)
    assert answer.answered is True
    assert "502" in (answer.reason or "")


def test_a_refused_dial_is_unanswered() -> None:
    """Nothing answered: the respawnable DOWN, not the identity branch."""
    answer = probe_mod.cdp_unreachable(_closed_port())
    assert answer.reason is not None
    assert answer.answered is False


def test_cdp_200_with_valid_version_body_is_reachable() -> None:
    body = (
        b'{"Browser": "Chrome/150.0.0.0", "Protocol-Version": "1.3", '
        b'"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/x"}'
    )
    with _serve_cdp_once(200, body) as port:
        assert probe_mod.cdp_unreachable(port).reason is None


def test_fake_alive_verdict_is_down_even_when_our_chrome_listens() -> None:
    """2026-09-09 macmini: our orphaned Chrome still LISTENs on the port and
    answers 200 with an empty body. The probe must read DOWN (so the
    healthcheck's sweep + rebuild runs), never ALIVE."""
    answer = probe_mod.parse_cdp_version_body(probe_mod.cdp_url(9222), 200, b"")
    verdict = probe_mod.decide_browser_probe(
        9222,
        _PROFILE,
        answer=answer,
        found=[42],
        listening={42: True},
        holder=42,
    )
    assert verdict.verdict is ProbeVerdict.DOWN
    assert "wedged" in verdict.detail


def test_garbage_200_with_our_own_wedged_endpoint_is_down() -> None:
    """The 2026-09-09 shape with the identity arms stubbed plainly: our Chrome
    holds the port, so the sweep + rebuild can heal it — DOWN, never terminal."""
    verdict = _decide(
        cdp="CDP :9222 answered 200 but the body is not JSON — wedged DevTools endpoint",
        answered=True,
        chromes=[42],
        listening={42: True},
        holder=42,
    )
    assert verdict.verdict is ProbeVerdict.DOWN
    assert verdict.terminal is False
    assert "42" in verdict.detail
    assert "wedged" in verdict.detail


def test_garbage_200_with_a_foreign_listener_is_terminal() -> None:
    """An unusable answer still proves the port has an occupant: when the
    listener is not this cluster's Chrome, a respawn cannot bind the port and
    must not be attempted once every 60s (task #2692)."""
    verdict = _decide(
        cdp="CDP :9222 answered 200 but the body is not JSON — wedged DevTools endpoint",
        answered=True,
        chromes=[],
        listening={},
        holder=999,
        holder_facts=("chrome", ["--user-data-dir=/somebody-elses-profile"]),
    )
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert verdict.terminal is True


def test_garbage_200_with_nobody_identifiable_is_terminal() -> None:
    """Nothing identifiable holds the port but something answered: terminal, the
    same line the valid-payload path draws — fail closed, never churn."""
    verdict = _decide(
        cdp="CDP :9222 returned HTTP 502",
        answered=True,
        chromes=[],
        listening={},
    )
    assert verdict.verdict is ProbeVerdict.PORT_TAKEN
    assert verdict.terminal is True


def test_garbage_200_listener_identified_ours_by_its_own_argv() -> None:
    """The listener-first arm still applies on the unusable-payload branch: a
    holder the profile walk missed is ours when its own argv says so."""
    verdict = _decide(
        cdp="CDP :9222 answered 200 but the body is not JSON — wedged DevTools endpoint",
        answered=True,
        chromes=[],  # the walk missed it
        listening={},
        holder=42,
        holder_facts=(
            "Google Chrome",
            [f"--user-data-dir={_PROFILE}", "--remote-debugging-port=9222"],
        ),
    )
    assert verdict.verdict is ProbeVerdict.DOWN
    assert "42" in verdict.detail


def test_unanswered_cdp_stays_the_plain_respawnable_down() -> None:
    """A refused dial proves nothing answered — no occupant to identify, so the
    verdict is the plain DOWN a respawn can act on."""
    verdict = _decide(
        cdp="CDP unreachable on http://127.0.0.1:9222/json/version: URLError: refused",
        answered=False,
        chromes=[42],
        listening={42: False},
    )
    assert verdict.verdict is ProbeVerdict.DOWN
    assert verdict.terminal is False


def test_cdp_url_is_the_one_definition() -> None:
    """The daemon's port guard, the healthcheck and this probe all dial the same
    endpoint; a second spelling is how they would drift apart."""
    import services.desktop.browser.daemon as daemon_mod

    assert probe_mod.cdp_url(9222) == "http://127.0.0.1:9222/json/version"
    assert daemon_mod.cdp_url is probe_mod.cdp_url
