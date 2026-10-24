# meiva:note
# One refused packet is not proof the gateway is down.
#
# The runner's self-update preflight is validate-before-kill: it dials the gateway and,
# on failure, declines with `RESTART_DECLINED_EXIT_CODE` rather than stopping services it
# might not be able to restart. Declining is right; declining on the *first* refused dial
# was not. On the 2026-08-01 rollout (issue #1151) a ~9 s gateway restart hole produced
# exactly one ECONNREFUSED on two runners, and both sat stranded until the settle lease
# lapsed ~15 minutes later.
#
# The hole that opened it is closed at its source (`_phase_b_targets`); this budget is
# what remains for holes the rollout did not open. So the tests are about **what the
# budget must and must not buy**:
#
# - a gateway that comes back inside the budget is waited for, not declined;
# - a gateway that is genuinely down still fails, inside the bound, so `INCOMPLETE` stays
#   fast and legible rather than becoming slow;
# - the healthy path pays nothing — a budget that taxes every good `meiva start` would be a
#   bad trade even when it is correct;
# - a status the gateway *chose* to send is still terminal on the first dial.
#
# The dials are real: a scripted loopback HTTP server answers them, a bound-but-not-listening
# socket refuses them, and the retry interval is a few milliseconds so the budget is reached in
# real time that is still short.


from __future__ import annotations

import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from base.deploy.progress_timeout import GATEWAY_PREFLIGHT_BUDGET_S, NO_PROGRESS_TIMEOUT_S
from cli.commands import _repo
from cli.commands.observability.tests.scripted_http import Reply, serve

PROBE_PATH = _repo.GATEWAY_PROBE_PATH
INTERVAL_S = 0.02
BUDGET_S = 2.0


# meiva:note
# A gateway that refuses connections until `delay_s` has passed and then serves 200: the shape of
# a restart hole. The port is bound from the start (so nothing else can take it) but only
# listens later, which is what makes the early dials fail with a refusal.
@contextmanager
def _gateway_back_after(delay_s: float) -> Generator[str]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            del format, args

        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"{}")

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler, bind_and_activate=False)
    server.allow_reuse_address = True
    server.server_bind()

    listening = threading.Event()

    def open_listener() -> None:
        server.server_activate()
        listening.set()
        server.serve_forever()

    serving = threading.Timer(delay_s, open_listener)
    serving.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        if listening.wait(delay_s + 5.0):
            server.shutdown()
        serving.join()
        server.server_close()


# meiva:note
# The defect. A restart hole a few seconds wide used to cost the host its whole
# rollout; now a later dial finds the gateway and the self-update proceeds.
def test_a_gateway_that_comes_back_inside_the_budget_is_not_declined() -> None:
    with _gateway_back_after(0.15) as url:
        started = time.monotonic()
        rc = _repo.probe_gateway_or_die(url, budget_s=BUDGET_S, retry_interval_s=INTERVAL_S)

    assert rc == 0
    assert time.monotonic() - started < BUDGET_S


# meiva:note
# The budget must not become a way to survive a gateway that really died — that
# needs the watchdog's own round, and waiting for it here would turn a legible
# INCOMPLETE into a slow one. It fails, having spent no more than the budget.
def test_a_dead_gateway_still_fails_and_inside_the_bound() -> None:
    from base.host.net.tests.local_http import closed_port_url

    budget_s = 0.2
    started = time.monotonic()
    rc = _repo.probe_gateway_or_die(
        closed_port_url(), budget_s=budget_s, retry_interval_s=INTERVAL_S
    )

    assert rc == 1
    assert time.monotonic() - started < budget_s + 1.0


# meiva:note
# Every healthy `meiva start` and every healthy self-update runs this. It must be
# one probe and zero waiting, exactly as before the budget existed.
def test_a_reachable_gateway_costs_one_dial_and_no_sleep() -> None:
    with serve({PROBE_PATH: Reply(status=200, body=b"{}")}) as gateway:
        started = time.monotonic()
        rc = _repo.probe_gateway_or_die(gateway.url, budget_s=BUDGET_S, retry_interval_s=10.0)
        elapsed = time.monotonic() - started

    assert rc == 0
    assert len(gateway.received) == 1
    assert elapsed < 5.0


# meiva:note
# A gateway that answers 5xx is booting, which is the same transient shape as one
# that answers nothing — it was already retried, and it keeps the same budget rather
# than a second, separately-tuned one.
def test_a_5xx_is_retried_on_the_same_budget() -> None:
    script = [Reply(status=503, body=b"starting"), Reply(status=200, body=b"{}")]
    with serve({PROBE_PATH: script}) as gateway:
        rc = _repo.probe_gateway_or_die(gateway.url, budget_s=BUDGET_S, retry_interval_s=INTERVAL_S)

    assert rc == 0
    assert len(gateway.received) == 2


# meiva:note
# 401/403/404 mean a credential or route mismatch: the gateway is up and saying no.
# No amount of waiting resolves it, so it must not spend the budget.
def test_a_refusal_the_gateway_chose_is_terminal_at_once() -> None:
    with serve({PROBE_PATH: Reply(status=403, body=b"forbidden")}) as gateway:
        rc = _repo.probe_gateway_or_die(gateway.url, budget_s=BUDGET_S, retry_interval_s=INTERVAL_S)

    assert rc == 1
    assert len(gateway.received) == 1


# meiva:note
# This wait happens inside the self-update leg that `NO_PROGRESS_TIMEOUT_S` bounds
# whole. If it ever approached that number, absorbing a restart hole would be
# indistinguishable from the host having stopped making progress.
def test_the_budget_sits_far_under_the_no_progress_bound() -> None:
    assert GATEWAY_PREFLIGHT_BUDGET_S < NO_PROGRESS_TIMEOUT_S / 10
