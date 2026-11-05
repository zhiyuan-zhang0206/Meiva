#!/usr/bin/env python3
# meiva:note
# The alert-pipeline sentinel that runs outside Grafana (task #6220).
# meiva:note
# The watchdog that proves the alert path works lives inside Grafana's alert engine: when the
# engine stops evaluating, the watchdog stops with it and nothing reports. This asks the same
# question from the outside -- how old is the newest accepted alert ingest -- straight against
# Prometheus, and submits a message through the installed Meiva CLI. It does not require Grafana.
# meiva:note
# Two rules it obeys. Absence is the failure, but a metric that has never existed is unarmed
# rather than broken: the first sighting of a sample is recorded and the sentinel stays quiet
# until then. And one incident is one message: the state file also holds the last send, so a
# fifteen-minute tick does not turn one outage into a stream.
# meiva:link scripts/host_ops/docs/watchdog-sentinel.meiva.okf.md
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractContextManager
from functools import partial
from http.client import HTTPResponse
from pathlib import Path
from typing import NamedTuple, TypeGuard

# meiva:note
# How old the newest accepted ingest may be before the path counts as silent. Grafana's own
# watchdog repeats every five minutes and its rule allows three periods, so thirty minutes is the
# same judgement made from outside with room for one missed tick.
DEFAULT_SILENCE_SECONDS = 1800.0

# meiva:note
# One message per incident, then at most one repeat per window: an outage that lasts a night
# should not become a hundred notifications, and an outage that is still there in the morning
# should not be a state nobody remembers.
DEFAULT_REPEAT_HOURS = 6.0

# meiva:note
# The installed CLI wakes the named owner through the existing agent message path.
DEFAULT_ESCALATE_AGENT = 405
ESCALATION_SOURCE = "external_agent:watchdog-sentinel"

DEFAULT_PROM_URL = "http://127.0.0.1:59090/api/v1/query"
DEFAULT_QUERY = "time() - max(meiva_alerts_last_ingest_timestamp_seconds)"


def as_object[**P](call: Callable[P, object], /, *args: P.args, **kwargs: P.kwargs) -> object:
    # meiva:note
    # The standard library types `json.load` and `urlopen` as `Any`. Handing the function to a
    # parameter of this shape keeps the untyped answer outside the callee, so every name here holds
    # `object` and the shape is narrowed where it is read, never assumed. This and the two guards
    # below are a copy of `base.host.boundary.untyped` on purpose: this file must import nothing
    # outside the standard library, because it is the observer that runs when the runtime is the
    # thing that broke.
    return call(*args, **kwargs)


def is_json_object(value: object) -> TypeGuard[Mapping[str, object]]:
    # meiva:note
    # A JSON object, narrowed where it is read instead of cast. Its keys are strings by
    # construction of the format, which is what makes the narrower type sound.
    return isinstance(value, Mapping)


def is_json_array(value: object) -> TypeGuard[Sequence[object]]:
    return isinstance(value, Sequence) and not isinstance(value, str | bytes)


def http_json(
    target: str,
    *,
    open_url: Callable[..., AbstractContextManager[HTTPResponse]],
    data: bytes | None = None,
) -> object:
    # meiva:note
    # One request, answered as JSON: a GET when `data` is None and a form POST otherwise. The
    # opener arrives as a parameter because `urlopen` is typed `Any` at its call site; a caller
    # that prints the answer instead of reading it is the caller's business.
    with open_url(target, data=data) as response:
        return as_object(json.load, response)


def age_from_payload(payload: object) -> float | None:
    # meiva:note
    # Prometheus answers a vector; an empty result is "this metric has never been pushed", which
    # is not the same as zero age and is not an age at all.
    if not is_json_object(payload):
        return None
    data = payload.get("data")
    if not is_json_object(data):
        return None
    result = data.get("result")
    if not is_json_array(result) or not result:
        return None
    first = result[0]
    if not is_json_object(first):
        return None
    value = first.get("value")
    if not is_json_array(value) or len(value) != 2:
        return None
    newest = value[1]
    return float(newest) if isinstance(newest, str | int | float) else None


class Endpoints(NamedTuple):
    # meiva:note
    # The metric read and the existing agent-message submission are independently controlled.
    age: Callable[[], float | None]
    send: Callable[[str], bool]


class Sentinel:
    # meiva:note
    # One installation: where its state, log and credentials live, how Prometheus is asked, and
    # which channel carries an escalation. Every input a test must control is a constructor
    # argument, so a test drives a real tick against a temporary home instead of patching this
    # module.
    def __init__(
        self,
        *,
        home: Path,
        state_path: Path | None = None,
        log_path: Path | None = None,
        prom_url: str = DEFAULT_PROM_URL,
        query: str = DEFAULT_QUERY,
        meiva_bin: Path | None = None,
        escalate_agent: int = DEFAULT_ESCALATE_AGENT,
        silence_seconds: float = DEFAULT_SILENCE_SECONDS,
        repeat_hours: float = DEFAULT_REPEAT_HOURS,
        dry_run: bool = False,
        endpoints: Endpoints | None = None,
    ) -> None:
        self.home = home
        self.state_path = state_path or home / "ops" / "watchdog-sentinel.state"
        self.log_path = log_path or home / "ops" / "watchdog-sentinel.log"
        self.prom_url = prom_url
        self.query = query
        self.meiva_bin = meiva_bin or home / "source" / ".venv" / "bin" / "meiva"
        self.escalate_agent = escalate_agent
        self.silence_seconds = silence_seconds
        self.repeat_hours = repeat_hours
        self.dry_run = dry_run
        self._endpoints = endpoints

    def endpoints(self) -> Endpoints:
        # meiva:note
        # The real legs, unless the caller supplied recorded ones. Built per tick rather than in
        # the constructor so a subclass-free test can still exercise every method above.
        if self._endpoints is not None:
            return self._endpoints
        return Endpoints(
            age=self.age_seconds,
            send=self.escalate,
        )

    def read_state(self) -> dict[str, float]:
        # meiva:note
        # The state file holds numbers only -- when a sample was first seen, when a message last
        # went out, and the streaks behind the two delivery legs. Anything else in it is stale or
        # hand-edited, so a value that is not a number is dropped rather than carried forward.
        try:
            text = self.state_path.read_text(encoding="utf-8")
        except OSError:
            return {}
        loaded = as_object(json.loads, text)
        if not is_json_object(loaded):
            return {}
        return {
            key: float(value) for key, value in loaded.items() if isinstance(value, int | float)
        }

    def write_state(self, state: dict[str, float]) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.write_text(json.dumps(state), encoding="utf-8")

    def note(self, line: str) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(f"{stamp} {line}\n")

    def age_seconds(self) -> float | None:
        # meiva:note
        # This installation's own endpoint: the scheme is fixed, the host comes from the
        # configured Prometheus and the only variable part is the encoded query. `urlopen` hands
        # back an `Any`, so it travels as a function of the declared shape instead of a value.
        url = self.prom_url + "?" + urllib.parse.urlencode({"query": self.query})
        opener = partial(urllib.request.urlopen, timeout=15)
        return age_from_payload(http_json(url, open_url=opener))

    def escalate(self, message: str) -> bool:
        # meiva:note
        # The delivery leg uses the installed CLI. `agents send` auto-resurrects a terminated target, so
        # naming the owner is enough. The argv is fixed (the installed CLI, a literal subcommand,
        # this sentinel's source name, an integer id, the message) and no shell is involved, which
        # is the S603 site below.
        command = [
            str(self.meiva_bin),
            "agents",
            "send",
            "--source",
            ESCALATION_SOURCE,
            str(self.escalate_agent),
            message,
        ]
        environment = {**os.environ, "MEIVA_HOME": str(self.home)}
        try:
            result = subprocess.run(  # noqa: S603
                command,
                capture_output=True,
                text=True,
                timeout=120,
                env=environment,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            self.note(f"ERROR escalation failed: {error}")
            return False
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip()[:200]
            self.note(f"ERROR escalation rc={result.returncode}: {detail}")
            return False
        self.note("submitted to the agent message queue")
        return True

    def tick(self, now: float | None = None) -> int:
        stamp = time.time() if now is None else now
        wiring = self.endpoints()
        state = self.read_state()
        try:
            age = wiring.age()
        except (OSError, ValueError) as error:
            self.note(f"ERROR prometheus query failed: {error}")
            return 1
        if age is None:
            self.note("unarmed no sample yet")
            return 0
        state.setdefault("first_seen", stamp)
        if age <= self.silence_seconds:
            self.note(f"ok age={age:.0f}s")
            self.write_state(state)
            return 0
        self.note(f"SILENT age={age:.0f}s")
        last_sent = state.get("last_sent")
        if last_sent is not None and stamp - last_sent < self.repeat_hours * 3600:
            self.write_state(state)
            return 0
        text = (
            f"ALERT: the alert pipeline has been silent for over {int(self.silence_seconds)}s "
            f"(newest ingest sample is {int(age)}s old). "
            "Check the gateway and Grafana alert engine. (host sentinel, task #6220)"
        )
        if self.dry_run:
            print(f"would submit: {text}")
            self.write_state(state)
            return 0
        if not wiring.send(text):
            self.note("ERROR agent message was not accepted; delivery remains pending")
            self.write_state(state)
            return 1
        state["last_sent"] = stamp
        self.write_state(state)
        return 0


class _Arguments(argparse.Namespace):
    # meiva:note
    # The parsed flags as a typed namespace: `parse_args` answers `Any` through a plain Namespace,
    # and every value below is a flag this file declares, so the names are declared once here.
    home: str
    silence_seconds: float
    repeat_hours: float
    prom_url: str
    escalate_agent: int
    dry_run: bool


def main(argv: list[str]) -> int:
    home = Path.home() / ".meiva"
    description = "Ask, from outside Grafana, how old the newest accepted alert ingest is."
    parser = argparse.ArgumentParser(description=description)
    parser.add_argument("--home", default=str(home))
    parser.add_argument("--silence-seconds", type=float, default=DEFAULT_SILENCE_SECONDS)
    parser.add_argument("--repeat-hours", type=float, default=DEFAULT_REPEAT_HOURS)
    parser.add_argument("--prom-url", default=DEFAULT_PROM_URL)
    parser.add_argument("--escalate-agent", type=int, default=DEFAULT_ESCALATE_AGENT)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv, namespace=_Arguments())
    sentinel = Sentinel(
        home=Path(args.home).expanduser(),
        prom_url=args.prom_url,
        escalate_agent=args.escalate_agent,
        silence_seconds=args.silence_seconds,
        repeat_hours=args.repeat_hours,
        dry_run=args.dry_run,
    )
    return sentinel.tick()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
