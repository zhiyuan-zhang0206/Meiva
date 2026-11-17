# meiva:note
# Schedule runner entrypoint for meiva_usage collectors.
# meiva:link meiva_builtins/plugins/meiva_usage/README.md

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.rows import query_one
from base.host.boundary.json_values import is_object_dict
from base.host.boundary.untyped import object_of
from base.log import logger
from base.packages.plugins.config_view import disk_image_path, read_plugin_config
from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError
from base.packages.plugins.surface import stats
from base.packages.plugins.surface.stats_types import StatReset
from meiva_builtins.plugins.meiva_usage.backoff import judge_cycle, next_interval_s
from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig, UsageConfig
from meiva_builtins.plugins.meiva_usage.probes import (
    CodexRpcRunner,
    HttpJsonFetcher,
    probe_collector,
)
from meiva_builtins.plugins.meiva_usage.types import (
    CardReading,
    UsageRateLimitedError,
    should_write_failure,
)

_log = logger

# meiva:note
# The value a card shows when it has never held a reading.
_NO_READING = "—"


@dataclass(frozen=True)
class _PacingState:
    consecutive_rate_limits: int = 0
    not_before: datetime | None = None


def _load_pacing_state(path: Path) -> _PacingState:
    try:
        payload = object_of(json.loads, path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return _PacingState()
    if not is_object_dict(payload):
        return _PacingState()
    raw_count = payload.get("consecutive_rate_limits")
    raw_not_before = payload.get("not_before")
    count = raw_count if type(raw_count) is int and raw_count >= 0 else 0
    if not isinstance(raw_not_before, str):
        return _PacingState(consecutive_rate_limits=count)
    try:
        not_before = datetime.fromisoformat(raw_not_before.replace("Z", "+00:00"))
    except ValueError:
        return _PacingState(consecutive_rate_limits=count)
    if not_before.tzinfo is None:
        return _PacingState(consecutive_rate_limits=count)
    return _PacingState(count, not_before.astimezone(UTC))


def _save_pacing_state(path: Path, state: _PacingState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(
        json.dumps(
            {
                "consecutive_rate_limits": state.consecutive_rate_limits,
                "not_before": (
                    state.not_before.astimezone(UTC).isoformat()
                    if state.not_before is not None
                    else None
                ),
            },
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


# meiva:note
# The cached card row, selected by its primary key. Every column is NOT NULL in `plugin_stats`, so
# a found row always carries all three and only a missing row means "nothing cached".
@dataclass(frozen=True)
class _CachedCard:
    status: str
    updated_at: datetime
    value: str
    resets: list[dict[str, str]]


def _resolve_error_hysteresis(
    db: Database,
    card_id: str,
    reading: CardReading,
    now: datetime,
) -> tuple[CardReading, bool]:
    with db.write_transaction() as conn:
        cached = query_one(
            conn,
            _CachedCard,
            "SELECT status, updated_at, value, resets FROM plugin_stats WHERE plugin = %s AND id = %s",
            ("meiva_usage", card_id),
        )
    existing_status = None if cached is None else cached.status
    existing_updated_at = None if cached is None else cached.updated_at
    existing_value = cached.value if cached is not None and cached.value else None

    if not should_write_failure(existing_status, existing_updated_at, now):
        status = existing_status if existing_status in ("ok", "warn", "error") else "ok"
        return (
            CardReading(
                value=existing_value or "—",
                detail="probe error suppressed (cached reading fresh)",
                resets=tuple(StatReset.model_validate(r) for r in cached.resets) if cached else (),
                status=status,
            ),
            False,
        )
    if existing_value:
        return CardReading(
            value=existing_value,
            detail=reading.detail,
            status="error",
            resets=tuple(StatReset.model_validate(r) for r in cached.resets) if cached else (),
        ), True
    return reading, True


# meiva:note
# A rate-limited probe is not a failed one: the card keeps its last reading and the age of that
# reading (`refresh_stamp=False`), and only its status and detail say the number is old. The detail
# names the as-of instant, because a stamp alone would read as "refreshed just now" in a log line.
# With no earlier reading there is nothing to keep, so the placeholder reading is written as is.
def _rate_limited_reading(
    db: Database,
    card_id: str,
    reading: CardReading,
) -> tuple[CardReading, bool]:
    with db.write_transaction() as conn:
        cached = query_one(
            conn,
            _CachedCard,
            "SELECT status, updated_at, value, resets FROM plugin_stats WHERE plugin = %s AND id = %s",
            ("meiva_usage", card_id),
        )
    if cached is None or not cached.value or cached.value == _NO_READING:
        return reading, False
    as_of = cached.updated_at.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC")
    return (
        CardReading(
            value=cached.value,
            detail=f"Rate limited by the provider (HTTP 429); showing the reading from {as_of}",
            status="warn",
            rate_limited=True,
            resets=tuple(StatReset.model_validate(r) for r in cached.resets),
        ),
        True,
    )


def _upsert_card_reading(
    db: Database,
    *,
    card_id: str,
    reading: CardReading,
    now: datetime,
    machine_name: str,
) -> CardReading:
    keep_stamp = False
    if reading.rate_limited:
        reading, keep_stamp = _rate_limited_reading(db, card_id, reading)
    elif reading.status == "error":
        reading, should_upsert = _resolve_error_hysteresis(db, card_id, reading, now)
        if not should_upsert:
            return reading

    stats.upsert(
        db,
        plugin="meiva_usage",
        id=card_id,
        value=reading.value,
        detail=reading.detail,
        status=reading.status,
        updated_by=machine_name,
        detail_cap=500,
        refresh_stamp=not keep_stamp,
        resets=reading.resets,
    )
    return reading


def _error_detail(exc: Exception) -> str:
    err_msg = str(exc).strip() or type(exc).__name__
    if len(err_msg) > 200:
        err_msg = err_msg[:197] + "..."
    return err_msg


def refresh_collector_once(
    collector: CollectorConfig,
    *,
    db: Database,
    user_home: Path,
    environ: Mapping[str, str],
    now: datetime,
    platform: str,
    display_tz: tzinfo = UTC,
    machine_name: str = "local",
    http_fetch: HttpJsonFetcher | None = None,
    keychain_reader: Callable[[str], str | None] | None = None,
    codex_rpc_runner: CodexRpcRunner | None = None,
) -> dict[str, CardReading]:
    readings: dict[str, CardReading]
    try:
        readings = probe_collector(
            collector,
            user_home=user_home,
            environ=environ,
            now=now,
            platform=platform,
            display_tz=display_tz,
            http_fetch=http_fetch,
            keychain_reader=keychain_reader,
            codex_rpc_runner=codex_rpc_runner,
        )
    except UsageRateLimitedError as exc:
        readings = {
            card.id: CardReading(
                value=_NO_READING, detail=_error_detail(exc), status="warn", rate_limited=True
            )
            for card in collector.stat_cards()
        }
    except Exception as exc:
        readings = {
            card.id: CardReading(value=_NO_READING, detail=_error_detail(exc), status="error")
            for card in collector.stat_cards()
        }

    results: dict[str, CardReading] = {}
    for card in collector.stat_cards():
        reading = readings.get(card.id)
        if reading is None:
            reading = CardReading(
                value=_NO_READING,
                detail=f"Card '{card.id}' missing from probe results",
                status="error",
            )
        persisted = _upsert_card_reading(
            db,
            card_id=card.id,
            reading=reading,
            now=now,
            machine_name=machine_name,
        )
        results[card.id] = persisted
    return results


def _resolve_display_tz(display_tz: tzinfo | None, environ: Mapping[str, str]) -> tzinfo:
    if display_tz is not None:
        return display_tz
    tz_name = environ.get("MEIVA_TIMEZONE", "UTC")
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError, ValueError):
        return UTC


def _load_collector_config(
    collector_id: str,
    home: Path,
    environ: Mapping[str, str],
) -> CollectorConfig | None:
    config_path = disk_image_path("meiva_usage", home=home)
    try:
        cfg = read_plugin_config(
            "meiva_usage",
            UsageConfig,
            config_path,
            env=environ,
        )
    except (InvalidConfigData, SchemaDriftError) as exc:
        _log.warning("meiva_usage invalid config for collector {}: {}", collector_id, exc)
        return None
    except Exception as exc:
        _log.warning("meiva_usage failed to read config for collector {}: {}", collector_id, exc)
        return None

    collector = next((c for c in cfg.collectors if c.id == collector_id), None)
    if collector is None or not collector.enabled:
        return None
    return collector


# meiva:note
# The process every collector's events are attributed to. One name for the whole fleet's schedules,
# not one per schedule: `process` is the OTLP service name, and a name per schedule would flatten
# the metric surface across them. Which schedule an event came from rides in the attributes.
SCHEDULE_PROCESS_NAME = "schedule-runner"


# meiva:note
# A schedule script runs inside the runner process, which owns an event pipeline that was never
# bound into the emitter: `emit()` therefore dropped every event the loop produced, and seven days
# of collector cycles left no row anywhere (2026-11-06, task #6352). Binding is the entry's act,
# not the emitter's — an observation must not change process state to make itself work.
# One bind per process: `init_telemetry` refuses a second live pipeline, and the runner process runs
# a loop for as long as the schedule lives. A bind that fails must not stop the collector, so it is
# reported and the loop carries on; the per-cycle report below then says the events are dropped.
def _bind_schedule_telemetry(borrowed: ScheduleInputs, collector_id: str) -> None:
    from base.telemetry.emitter import pipeline_bound
    from base.telemetry.process_logging import init_gateway_process

    if pipeline_bound():
        return
    try:
        init_gateway_process(
            SCHEDULE_PROCESS_NAME,
            producer=borrowed.producer,
            machine_reader=borrowed.machine.name,
            image=borrowed.image,
            home=borrowed.home,
        )
    except Exception as exc:
        _log.warning("collector {} cannot bind its event pipeline: {}", collector_id, exc)


# meiva:note
# The schedule this process runs, as the runner names it in the environment. A manual run has none,
# and the attribute is optional for exactly that case: a missing id is not a misattributed one.
def _schedule_id() -> int | None:
    raw = os.environ.get("MEIVA_SCHEDULE_ID", "").strip()
    return int(raw) if raw.isdigit() else None


# meiva:note
# One event per refresh cycle, in both outcomes. The loop's exception handler is the only place a
# failed cycle is visible, and until this existed "alive but producing nothing" was
# indistinguishable from healthy (schedule 50, 2026-11-04: three hours of swallowed failures).
# Telemetry must never break the loop: a process with no pipeline, or an event the registry
# refuses, is reported to the log and the next cycle still runs.
# `False` is "this process has no pipeline, the event did not leave it": the drop is the loop's to
# report, because the loop is what knows whether it has already said so (2026-11-06, task #6348).
def _emit_cycle(collector_id: str, outcome: str, reason: str, *, cycle: int) -> bool:
    from base.telemetry.emitter import emit, pipeline_bound

    if not pipeline_bound():
        return False
    attributes: dict[str, object] = {
        "collector": collector_id,
        "outcome": outcome,
        "reason": reason,
        "cycle": cycle,
    }
    schedule_id = _schedule_id()
    if schedule_id is not None:
        attributes["schedule_id"] = schedule_id
    try:
        emit("telemetry", "plugin_collector_cycle", attributes=attributes)
    except Exception as exc:
        _log.warning("collector {} cycle event not emitted: {}", collector_id, exc)
    return True


# meiva:note
# One line per backed-off cycle, so a collector that is quiet because it is throttled says so.
def _report_backoff(collector_id: str, rate_limited_cycles: int, interval_s: float) -> None:
    if rate_limited_cycles:
        _log.warning(
            "collector {} rate limited {} cycle(s) in a row; next cycle in {:.0f}s",
            collector_id,
            rate_limited_cycles,
            interval_s,
        )


# meiva:note
# The loop, with its two effects handed in as required parameters. The public entry point below
# supplies the real ones; a test supplies its own, which is why they carry no defaults — a default
# reaching the real implementation is a seam no caller sees.
def run_collector_cycles(
    borrowed: ScheduleInputs | None,
    *,
    refresh: Callable[..., Mapping[str, CardReading]],
    sleep: Callable[[float], None],
    collector_id: str,
    stop_after_cycles: int | None = None,
    initial_rate_limited_cycles: int = 0,
    environ: Mapping[str, str] | None = None,
    user_home: Path | None = None,
    platform: str | None = None,
    display_tz: tzinfo | None = None,
    http_fetch: HttpJsonFetcher | None = None,
    keychain_reader: Callable[[str], str | None] | None = None,
    codex_rpc_runner: CodexRpcRunner | None = None,
) -> int | None:
    if borrowed is None:
        raise ValueError("run_from_schedule requires ScheduleInputs (MEIVA_SCHEDULE_INPUTS)")
    _bind_schedule_telemetry(borrowed, collector_id)
    home = borrowed.home
    db = borrowed.database()
    machine_name = borrowed.machine.name()

    effective_home = Path.home() if user_home is None else user_home
    effective_platform = sys.platform if platform is None else platform
    effective_environ = os.environ if environ is None else environ
    effective_tz = _resolve_display_tz(display_tz, effective_environ)

    cycles = 0
    if initial_rate_limited_cycles < 0:
        raise ValueError("initial_rate_limited_cycles must not be negative")
    rate_limited_cycles = initial_rate_limited_cycles
    # meiva:note
    # A process with no bound event pipeline drops every event it is asked to emit, and until
    # 2026-11-06 it dropped them without a word: a schedule process never binds the pipeline
    # (`schedules/jobs/entry.py` builds a database entry and no telemetry; `init_telemetry()`
    # belongs to daemons), so "three days, zero cycle events" read exactly like "nothing
    # happened". One line, at the first drop of this run, is the whole cost: the loop owns the
    # flag because the loop is the scope that said it, and a per-cycle line would be 96 a day.
    unbound_reported = False
    while True:
        collector = _load_collector_config(collector_id, home, effective_environ)
        if collector is None:
            return None

        now = datetime.now(tz=UTC)
        try:
            readings = refresh(
                collector,
                db=db,
                user_home=effective_home,
                environ=effective_environ,
                now=now,
                platform=effective_platform,
                display_tz=effective_tz,
                machine_name=machine_name,
                http_fetch=http_fetch,
                keychain_reader=keychain_reader,
                codex_rpc_runner=codex_rpc_runner,
            )
        except Exception as exc:
            _log.warning("collector {} refresh cycle failed: {}", collector_id, exc)
            rate_limited_cycles = 0
            emitted = _emit_cycle(collector_id, "failed", type(exc).__name__, cycle=cycles)
        else:
            rate_limited_cycles, outcome, reason = judge_cycle(readings, rate_limited_cycles)
            emitted = _emit_cycle(collector_id, outcome, reason, cycle=cycles)

        if not emitted and not unbound_reported:
            unbound_reported = True
            _log.warning(
                "collector {} cycle events cannot be emitted: no telemetry pipeline is bound in "
                "this process (the schedule entry could not bind one), so every event this loop "
                "produces is dropped. The loop's liveness is readable from "
                "plugin_stats.updated_at.",
                collector_id,
            )

        cycles += 1
        base_interval_s = float(collector.interval_minutes * 60)
        interval_s = next_interval_s(base_interval_s, rate_limited_cycles)
        _report_backoff(collector_id, rate_limited_cycles, interval_s)
        if stop_after_cycles is not None and cycles >= stop_after_cycles:
            return rate_limited_cycles

        sleep(interval_s)


def run_from_schedule(borrowed: ScheduleInputs | None, *, collector_id: str) -> None:
    run_declarative_cycle(
        borrowed,
        collector_id=collector_id,
        state_path=Path("state.json"),
        clock=lambda: datetime.now(tz=UTC),
    )


def run_declarative_cycle(
    borrowed: ScheduleInputs | None,
    *,
    collector_id: str,
    state_path: Path,
    clock: Callable[[], datetime],
    refresh: Callable[..., Mapping[str, CardReading]] = refresh_collector_once,
    environ: Mapping[str, str] | None = None,
    user_home: Path | None = None,
    platform: str | None = None,
    display_tz: tzinfo | None = None,
    http_fetch: HttpJsonFetcher | None = None,
    keychain_reader: Callable[[str], str | None] | None = None,
    codex_rpc_runner: CodexRpcRunner | None = None,
) -> None:
    if borrowed is None:
        raise ValueError("run_from_schedule requires ScheduleInputs (MEIVA_SCHEDULE_INPUTS)")
    effective_environ = os.environ if environ is None else environ
    collector = _load_collector_config(collector_id, borrowed.home, effective_environ)
    if collector is None:
        return
    state = _load_pacing_state(state_path)
    now = clock().astimezone(UTC)
    if state.not_before is not None and now < state.not_before:
        _log.info(
            "collector {} rate-limit backoff holds this declarative fire until {}",
            collector_id,
            state.not_before.isoformat(),
        )
        return

    consecutive = run_collector_cycles(
        borrowed,
        refresh=refresh,
        sleep=time.sleep,
        collector_id=collector_id,
        stop_after_cycles=1,
        initial_rate_limited_cycles=state.consecutive_rate_limits,
        environ=effective_environ,
        user_home=user_home,
        platform=platform,
        display_tz=display_tz,
        http_fetch=http_fetch,
        keychain_reader=keychain_reader,
        codex_rpc_runner=codex_rpc_runner,
    )
    if consecutive is None:
        return
    interval_s = next_interval_s(float(collector.interval_minutes * 60), consecutive)
    _save_pacing_state(
        state_path,
        _PacingState(
            consecutive_rate_limits=consecutive,
            not_before=clock().astimezone(UTC) + timedelta(seconds=interval_s),
        ),
    )
