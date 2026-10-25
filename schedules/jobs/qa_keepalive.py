# meiva:note
# The effects of the QA keepalive: probe the page, probe the remote process, carry the streaks in
# a JSON file under the running host's MEIVA_HOME, and wake the owner.
#
# The schedule runner executes this on the gateway host, not on the machine the page and the
# watcher live on, so both probes are remote and "cannot reach the host" must stay distinct from
# "the process is gone": an unreachable host reports unknown, never broken.
#
# Where to look and whom to wake are site values and arrive through `Settings.from_environ`.
# Provisioning rewrites a schedule's stored script from its template, so a site value baked into
# the template would be forced onto every site that runs it. A site that has not configured the
# keeper keeps the process alive and says so every round instead of exiting: the runner relaunches a
# lost process, and a keeper that died at start is indistinguishable from one that is watching.
# meiva:link schedules/README.md

from __future__ import annotations

import json
import subprocess
import time
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo

import meiva
from schedules.plans.keepalive_rule import (
    KeepaliveState,
    RoundContext,
    RoundPlan,
    plan_round,
)

__all__ = [
    "ENV_AGENT",
    "ENV_HOST",
    "ENV_PROCESS",
    "ENV_SSH_TARGETS",
    "ENV_URL",
    "Keepalive",
    "Settings",
    "check",
    "load_state",
    "probe_page",
    "probe_process",
    "round_once",
    "run_keepalive",
    "save_state",
]

_PROBE_TIMEOUT_S = 5.0
_SSH_RC_UNREACHABLE = 255
_SSH_RC_NO_MATCH = 1
_SSH_TIMEOUT_S = 15.0

ENV_URL = "MEIVA_QA_KEEPALIVE_URL"
ENV_AGENT = "MEIVA_QA_KEEPALIVE_AGENT"
ENV_HOST = "MEIVA_QA_KEEPALIVE_HOST"
ENV_PROCESS = "MEIVA_QA_KEEPALIVE_PROCESS"
ENV_SSH_TARGETS = "MEIVA_QA_KEEPALIVE_SSH_TARGETS"


# meiva:note
# The three required values are validated when a round builds them, and an unconfigured site is
# reported by name rather than replaced with a default: a keeper that silently watches nothing
# reports health forever.
@dataclass(frozen=True)
class Settings:
    health_url: str
    report_agent: int
    host: str
    process_pattern: str
    ssh_targets: tuple[str, ...]

    @classmethod
    def from_environ(cls, env: Mapping[str, str]) -> Settings:
        url = _required(env, ENV_URL)
        host = _required(env, ENV_HOST)
        pattern = env.get(ENV_PROCESS, "").strip()
        targets = tuple(
            part.strip() for part in env.get(ENV_SSH_TARGETS, "").split(",") if part.strip()
        )
        if pattern and not targets:
            raise RuntimeError(f"{ENV_PROCESS} is set but {ENV_SSH_TARGETS} names no host to probe")
        raw_agent = _required(env, ENV_AGENT)
        try:
            report_agent = int(raw_agent)
        except ValueError as exc:
            raise RuntimeError(
                f"{ENV_AGENT} must be a numeric agent id, got {raw_agent!r}"
            ) from exc
        if report_agent <= 0:
            raise RuntimeError(f"{ENV_AGENT} must be a positive agent id, got {report_agent}")
        return cls(
            health_url=url,
            report_agent=report_agent,
            host=host,
            process_pattern=pattern,
            ssh_targets=targets,
        )


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is not set; this site has not configured the keeper")
    return value


# meiva:note
# `state` must be a path on the host the runner runs on, which is not necessarily the host being
# watched. `zone` stamps the lines an operator reads; `hint` and `blind_hint` are the recovery
# texts for the two kinds of wake.
@dataclass(frozen=True)
class Keepalive:
    env: Mapping[str, str]
    state: Path
    zone: ZoneInfo
    hint: str
    blind_hint: str
    poll_seconds: float = 600.0


# meiva:note
# The clock is a required input, not something this module reads: `run_keepalive` is handed one,
# and every stamp and streak decision is then a function of that instant.
def _stamp(instant: datetime, zone: ZoneInfo) -> str:
    return f"[{instant.astimezone(zone):%Y-%m-%d %H:%M:%S %Z}]"


def load_state(keepalive: Keepalive, *, instant: datetime) -> KeepaliveState:
    try:
        payload = json.loads(keepalive.state.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return KeepaliveState()
    except (OSError, ValueError) as exc:
        print(f"{_stamp(instant, keepalive.zone)} state unreadable: {exc}", flush=True)
        return KeepaliveState()
    return KeepaliveState(
        fails=int(payload.get("fails") or 0),
        first_fail_at=payload.get("first_fail_at"),
        blind=int(payload.get("blind") or 0),
        blind_since=payload.get("blind_since"),
        last_wake_at=float(payload.get("last_wake_at") or 0.0),
        last_blind_wake_at=float(payload.get("last_blind_wake_at") or 0.0),
    )


# meiva:note
# A lost state disables both wakes, so a failed write is printed, never swallowed.
def save_state(keepalive: Keepalive, state: KeepaliveState, *, instant: datetime, ok: bool) -> None:
    try:
        keepalive.state.parent.mkdir(parents=True, exist_ok=True)
        keepalive.state.write_text(
            json.dumps(
                {
                    "fails": state.fails,
                    "first_fail_at": state.first_fail_at,
                    "blind": state.blind,
                    "blind_since": state.blind_since,
                    "last_wake_at": state.last_wake_at,
                    "last_blind_wake_at": state.last_blind_wake_at,
                    "last_round_at": instant.isoformat(timespec="seconds"),
                    "last_round_ok": ok,
                }
            ),
            encoding="utf-8",
        )
    except OSError as exc:
        print(
            f"{_stamp(instant, keepalive.zone)} state unwritable {keepalive.state}: {exc}",
            flush=True,
        )


# meiva:note
# `urlopen` reports a refused connection, a DNS failure and a non-2xx status as OSError
# subclasses, and a malformed URL as ValueError; anything else is a defect here and must not be
# mistaken for "the page is down".
def probe_page(settings: Settings) -> bool:
    try:
        with urllib.request.urlopen(settings.health_url, timeout=_PROBE_TIMEOUT_S) as resp:
            return resp.status == 200
    except (OSError, ValueError):
        return False


# meiva:note
# True when the process runs, False when a reachable host has no match, None when no target
# answered. Targets are tried in order; the first one that answers decides.
def probe_process(settings: Settings) -> bool | None:
    command = f"pgrep -f {settings.process_pattern} | head -1"
    for target in settings.ssh_targets:
        try:
            proc = subprocess.run(
                [
                    "ssh",
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    f"ConnectTimeout={int(_PROBE_TIMEOUT_S)}",
                    target,
                    command,
                ],
                capture_output=True,
                text=True,
                timeout=_SSH_TIMEOUT_S,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == _SSH_RC_UNREACHABLE:
            continue
        if proc.returncode == _SSH_RC_NO_MATCH:
            return False
        if proc.returncode == 0:
            return bool(proc.stdout.strip())
    return None


# meiva:note
# The round's problems, and whether the process probe was blind: a site that configured no
# process to watch is never blind, it simply has nothing to ask about.
def check(settings: Settings) -> tuple[list[str], bool]:
    problems = []
    if not probe_page(settings):
        problems.append(f"page health check {settings.health_url} is not 200")
    if not settings.process_pattern:
        return problems, False
    alive = probe_process(settings)
    if alive is False:
        problems.append(f"process {settings.process_pattern} is not running on {settings.host}")
    return problems, alive is None


def round_once(
    keepalive: Keepalive, settings: Settings, state: KeepaliveState, instant: datetime
) -> tuple[KeepaliveState, RoundPlan]:
    problems, blind = check(settings)
    # meiva:note
    # ISO goes in the state file, because `last_round_at` is the liveness criterion a machine
    # compares; the zone-stamped form appears only in the lines an operator reads.
    plan = plan_round(
        state,
        problems,
        blind=blind,
        now=instant.timestamp(),
        context=RoundContext(
            stamp=instant.isoformat(timespec="seconds"),
            host=settings.host,
            hint=keepalive.hint,
            blind_hint=keepalive.blind_hint,
        ),
    )
    save_state(keepalive, plan.state, instant=instant, ok=not problems and not blind)
    return plan.state, plan


def run_keepalive(keepalive: Keepalive, clock: Callable[[], datetime]) -> NoReturn:
    first = clock()
    print(
        f"{_stamp(first, keepalive.zone)} qa-keepalive starting; state {keepalive.state}",
        flush=True,
    )
    state = load_state(keepalive, instant=first)
    while True:
        instant = clock()
        try:
            settings = Settings.from_environ(keepalive.env)
        except RuntimeError as exc:
            print(f"{_stamp(instant, keepalive.zone)} not configured: {exc}", flush=True)
            time.sleep(keepalive.poll_seconds)
            continue
        try:
            state = _round(keepalive, settings, state, instant)
        # meiva:note
        # A round that raises is this keeper's own defect, not a fact about the watched site:
        # print it and keep the loop alive rather than letting one bad round end the watch.
        except Exception as exc:
            print(
                f"{_stamp(instant, keepalive.zone)} round failed: {type(exc).__name__}: {exc}",
                flush=True,
            )
        time.sleep(keepalive.poll_seconds)


# meiva:note
# One round and its wake, kept out of the loop so the loop reads as cadence rather than logic.
def _round(
    keepalive: Keepalive,
    settings: Settings,
    state: KeepaliveState,
    instant: datetime,
) -> KeepaliveState:
    state, plan = round_once(keepalive, settings, state, instant)
    if plan.wake is not None:
        print(
            f"{_stamp(instant, keepalive.zone)} waking {settings.report_agent}",
            flush=True,
        )
        meiva.agents.send_message(settings.report_agent, plan.wake)
    return state
