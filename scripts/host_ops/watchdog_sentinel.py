#!/usr/bin/env python3
# meiva:note
# The alert-pipeline sentinel that runs outside Grafana (task #6220).
# meiva:note
# The watchdog that proves the alert path works lives inside Grafana's alert engine: when the
# engine stops evaluating, the watchdog stops with it and nothing reports. This asks the same
# question from the outside -- how old is the newest accepted alert ingest -- straight against
# Prometheus, and posts to Telegram itself, so a dead Grafana cannot silence it.
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
import urllib.error
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
# Three failures in a row, or any permanent one, is when a human hears about it over a channel
# that is not the suspected leg. The target is the Meiva owner: `agents send` wakes it, so no
# liveness of any agent is assumed here.
ESCALATE_AFTER = 3
DEFAULT_ESCALATE_AGENT = 405
ESCALATION_SOURCE = "external_agent:watchdog-sentinel"

# meiva:note
# A failed send has two classes: transient (status 0, the network) and permanent (401 a bad token,
# 403 the user blocked the bot). Only the second is certain breakage -- waiting for it to heal
# spends the hours this sentinel exists to protect.
PERMANENT_STATUS = (401, 403)

# meiva:note
# The names this home actually defines are MEIVA_TELEGRAM_BOT_TOKEN and MEIVA_TELEGRAM_OWNER_ID;
# the MEIVA_ALERTS_* spelling exists only inside the Grafana contact point's own environment. Both
# are tried, because a sentinel that reads the wrong pair finds nothing and fails on exactly the
# night it is needed.
TELEGRAM_BOT_NAMES = ("MEIVA_ALERTS_TELEGRAM_BOT_TOKEN", "MEIVA_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_NAMES = ("MEIVA_ALERTS_TELEGRAM_CHAT_ID", "MEIVA_TELEGRAM_OWNER_ID")

DEFAULT_PROM_URL = "http://127.0.0.1:59090/api/v1/query"
DEFAULT_QUERY = "time() - max(meiva_alerts_last_ingest_timestamp_seconds)"
TELEGRAM_API = "https://api.telegram.org/bot"


class TelegramSendError(RuntimeError):
    # meiva:note
    # A failed Telegram call, carrying the status that decides whether it is transient (0) or
    # permanent (401, 403) and the provider's own words for the log line.
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"telegram call failed ({status}): {detail}")
        self.status = status
        self.detail = detail


def parse_env(text: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in text.splitlines():
        if "=" not in line or line.lstrip().startswith("#"):
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def pick(values: dict[str, str], names: tuple[str, ...]) -> str:
    for name in names:
        if values.get(name):
            return values[name]
    return ""


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
    # The four effects one tick needs: how old the newest sample is, how to send over Telegram,
    # how to check that send leg's capability, and how to reach the owner when the leg itself is
    # broken. They are a constructor field so a test runs the real decision path with recorded
    # effects -- replacing a method by name would keep testing the override, not this class.
    age: Callable[[], float | None]
    send: Callable[[str], object]
    probe: Callable[[], None]
    escalate: Callable[[str], bool]


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
        env_path: Path | None = None,
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
        self.env_path = env_path or home / ".env"
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
            send=self.telegram,
            probe=self.probe_delivery,
            escalate=self.escalate,
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

    def env_value(self, names: tuple[str, ...]) -> str:
        try:
            text = self.env_path.read_text(encoding="utf-8")
        except OSError:
            return ""
        return pick(parse_env(text), names)

    def age_seconds(self) -> float | None:
        # meiva:note
        # This installation's own endpoint: the scheme is fixed, the host comes from the
        # configured Prometheus and the only variable part is the encoded query. `urlopen` hands
        # back an `Any`, so it travels as a function of the declared shape instead of a value.
        url = self.prom_url + "?" + urllib.parse.urlencode({"query": self.query})
        opener = partial(urllib.request.urlopen, timeout=15)
        return age_from_payload(http_json(url, open_url=opener))

    def _telegram_call(self, url: str, body: bytes | None) -> object:
        # meiva:note
        # One HTTP call, three outcomes: an answer, a permanent refusal (the HTTP status the Bot
        # API returned) and a transient one (status 0, everything the network threw). The caller
        # reads the difference, not the exception's text. The scheme is fixed here (https to the
        # Bot API, http to the configured Prometheus) and no caller supplies a URL.
        opener = partial(urllib.request.urlopen, timeout=20)
        try:
            return http_json(url, open_url=opener, data=body)
        except urllib.error.HTTPError as error:
            detail = (error.read() or b"").decode("utf-8", "replace")[:200]
            raise TelegramSendError(error.code, detail) from error
        except (urllib.error.URLError, OSError) as error:
            raise TelegramSendError(0, str(error)[:200]) from error

    def telegram(self, text: str) -> object:
        # meiva:note
        # The Bot API's answer is the only receipt of a delivery: message_id and date are what a
        # report cites, and a caller that never saw them has proved nothing about delivery.
        token = self.env_value(TELEGRAM_BOT_NAMES)
        chat = self.env_value(TELEGRAM_CHAT_NAMES)
        if not token or not chat:
            raise TelegramSendError(401, "telegram credentials missing from the home's .env")
        url = f"{TELEGRAM_API}{token}/sendMessage"
        body = urllib.parse.urlencode({"chat_id": chat, "text": text}).encode()
        return self._telegram_call(url, body)

    def probe_delivery(self) -> None:
        # meiva:note
        # Capability without content: getMe fails on exactly the two permanent breaks and on
        # network loss, so it turns the send leg's six-hour sampling into a fifteen-minute one.
        # It is a warning, not proof -- a bot removed from the conversation can still answer
        # getMe -- which is why a real send's message_id stays the only evidence of the action.
        token = self.env_value(TELEGRAM_BOT_NAMES)
        if not token:
            raise TelegramSendError(401, "telegram credentials missing from the home's .env")
        answer = self._telegram_call(f"{TELEGRAM_API}{token}/getMe", None)
        if not is_json_object(answer) or not answer.get("ok"):
            raise TelegramSendError(0, "getMe answered not-ok")

    def escalate(self, message: str) -> bool:
        # meiva:note
        # The escalation leg is the agent channel, never Telegram: the suspected leg cannot carry
        # the news that it is broken. `agents send` auto-resurrects a terminated target, so
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
        self.note("ESCALATED telegram leg is broken; the owner was told over the agent channel")
        return True

    def tick(self, now: float | None = None) -> int:
        stamp = time.time() if now is None else now
        wiring = self.endpoints()
        state = self.read_state()
        # meiva:note
        # The capability check runs first, on every tick: a dead delivery leg makes every later
        # judgement unverifiable, and learning it here costs fifteen minutes instead of the six
        # hours between two alert attempts.
        probe_failure: TelegramSendError | None = None
        try:
            wiring.probe()
        except TelegramSendError as error:
            probe_failure = error
        probe_rc = self.record_probe(state, probe_failure, now=stamp)
        try:
            age = wiring.age()
        except (OSError, ValueError) as error:
            self.note(f"ERROR prometheus query failed: {error}")
            return 1
        if age is None:
            self.note("unarmed no sample yet (metric not deployed, or Prometheus is down)")
            return probe_rc
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
        if self.dry_run:
            print(f"would send: alert pipeline silent (age={age:.0f}s)")
            self.write_state(state)
            return 0
        text = (
            "ALERT: the alert pipeline has been silent for over "
            f"{int(self.silence_seconds)}s (newest ingest sample is {int(age)}s old).\n"
            "Grafana's own rules cannot report this -- they ride the same path. "
            "Check the gateway and the Grafana alert engine. (host sentinel, task #6220)"
        )
        try:
            receipt = wiring.send(text)
        except TelegramSendError as error:
            return self.record_send_failure(state, error, now=stamp)
        return self.record_send_success(state, receipt, now=stamp)

    def record_probe(
        self, state: dict[str, float], failure: TelegramSendError | None, *, now: float
    ) -> int:
        # meiva:note
        # The probe's verdict, kept apart from a real send's: this one is sampled every tick, so
        # three transient failures mean forty-five minutes, not the eighteen hours three alert
        # attempts would take. The escalation latch is shared by both legs, so a broken delivery
        # leg produces one message and not two.
        if failure is None:
            streak = int(state.get("probe_fail_streak", 0))
            if not streak:
                return 0
            started = float(state.get("probe_outage_started_at") or now)
            minutes = int((now - started) // 60)
            self.note(f"RECOVERED delivery probe after {streak} failure(s) / {minutes} min")
            if state.get("escalated_at"):
                try:
                    self.endpoints().send(
                        "Alert delivery recovered (probe): "
                        f"{streak} consecutive failures over {minutes} minutes."
                    )
                    self.note("recovery message sent over the restored leg")
                except TelegramSendError as error:
                    self.note(f"ERROR recovery message failed ({error.status}): {error.detail}")
            for key in ("probe_fail_streak", "probe_outage_started_at", "escalated_at"):
                state.pop(key, None)
            self.write_state(state)
            return 0
        streak = int(state.get("probe_fail_streak", 0)) + 1
        state["probe_fail_streak"] = streak
        state.setdefault("probe_outage_started_at", now)
        self.note(f"ERROR delivery probe failed [{streak}] ({failure.status}): {failure.detail}")
        permanent = failure.status in PERMANENT_STATUS
        if state.get("escalated_at") is None and (streak >= ESCALATE_AFTER or permanent):
            message = (
                f"The sentinel's delivery probe failed {streak} time(s), last "
                f"{failure.status}: {failure.detail}. This goes over the agent channel, not "
                f"Telegram; see {self.log_path}. (task #6220)"
            )
            if self.endpoints().escalate(message):
                state["escalated_at"] = now
        self.write_state(state)
        return 1

    def record_send_failure(
        self, state: dict[str, float], error: TelegramSendError, *, now: float
    ) -> int:
        streak = int(state.get("send_fail_streak", 0)) + 1
        state["send_fail_streak"] = streak
        state.setdefault("outage_started_at", now)
        self.note(f"ERROR telegram send failed [{streak}] ({error.status}): {error.detail}")
        permanent = error.status in PERMANENT_STATUS
        if state.get("escalated_at") is None and (streak >= ESCALATE_AFTER or permanent):
            message = (
                f"The sentinel cannot deliver over Telegram: {streak} failure(s), last "
                f"{error.status}: {error.detail}. This goes over the agent channel; see "
                f"{self.log_path}. (task #6220)"
            )
            if self.endpoints().escalate(message):
                state["escalated_at"] = now
        self.write_state(state)
        return 1

    def record_send_success(self, state: dict[str, float], receipt: object, *, now: float) -> int:
        # meiva:note
        # A delivery that worked after a failing streak is one event, not a reset: the log keeps
        # the streak and its length, and the user hears the leg is back -- otherwise they only
        # ever learn it broke, never that it healed.
        streak = int(state.get("send_fail_streak", 0))
        if streak:
            started = float(state.get("outage_started_at") or now)
            minutes = int((now - started) // 60)
            self.note(f"RECOVERED telegram send after {streak} failure(s) / {minutes} min")
            try:
                self.endpoints().send(
                    "Alert delivery recovered: "
                    f"{streak} consecutive send failures over {minutes} minutes."
                )
            except TelegramSendError as error:
                self.note(f"ERROR recovery message failed ({error.status}): {error.detail}")
            for key in ("send_fail_streak", "outage_started_at", "escalated_at"):
                state.pop(key, None)
        result = receipt.get("result") if is_json_object(receipt) else None
        message_id = result.get("message_id") if is_json_object(result) else None
        sent_at = result.get("date") if is_json_object(result) else None
        self.note(f"sent message_id={message_id} date={sent_at}")
        state["last_sent"] = now
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
