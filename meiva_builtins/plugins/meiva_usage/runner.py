# meiva:note
# Schedule runner entrypoint for meiva_usage collectors.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
from base.db.rows import query_one
from base.log import logger
from base.packages.plugins.config_face import disk_image_path
from base.packages.plugins.config_view import read_plugin_config
from base.packages.plugins.plugin_errors import InvalidConfigData, SchemaDriftError
from base.packages.plugins.surface import stats
from meiva_builtins.plugins.meiva_usage.default_config import CollectorConfig, UsageConfig
from meiva_builtins.plugins.meiva_usage.probes import (
    CodexRpcRunner,
    HttpJsonFetcher,
    probe_collector,
)
from meiva_builtins.plugins.meiva_usage.types import CardReading, should_write_failure

_log = logger


# meiva:note
# The cached card row, selected by its primary key. Every column is NOT NULL in `plugin_stats`, so
# a found row always carries all three and only a missing row means "nothing cached".
@dataclass(frozen=True)
class _CachedCard:
    status: str
    updated_at: datetime
    value: str


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
            "SELECT status, updated_at, value FROM plugin_stats WHERE plugin = %s AND id = %s",
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
                status=status,
            ),
            False,
        )
    if existing_value:
        return CardReading(value=existing_value, detail=reading.detail, status="error"), True
    return reading, True


def _upsert_card_reading(
    db: Database,
    *,
    card_id: str,
    reading: CardReading,
    now: datetime,
    machine_name: str,
) -> CardReading:
    if reading.status == "error":
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
    )
    return reading


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
    except Exception as exc:
        err_msg = str(exc).strip() or type(exc).__name__
        if len(err_msg) > 200:
            err_msg = err_msg[:197] + "..."
        readings = {
            card.id: CardReading(value="—", detail=err_msg, status="error")
            for card in collector.stat_cards()
        }

    results: dict[str, CardReading] = {}
    for card in collector.stat_cards():
        reading = readings.get(card.id)
        if reading is None:
            reading = CardReading(
                value="—",
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
    try:
        emit(
            "telemetry",
            "plugin_collector_cycle",
            attributes={
                "collector": collector_id,
                "outcome": outcome,
                "reason": reason,
                "cycle": cycle,
            },
        )
    except Exception as exc:
        _log.warning("collector {} cycle event not emitted: {}", collector_id, exc)
    return True


# meiva:note
# The loop, with its two effects handed in as required parameters. The public entry point below
# supplies the real ones; a test supplies its own, which is why they carry no defaults — a default
# reaching the real implementation is a seam no caller sees (diff-hints callable-default).
def run_collector_cycles(
    borrowed: ScheduleInputs | None,
    *,
    refresh: Callable[..., object],
    sleep: Callable[[float], None],
    collector_id: str,
    stop_after_cycles: int | None = None,
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
    home = borrowed.home
    db = borrowed.database()
    machine_name = borrowed.machine.name()

    effective_home = Path.home() if user_home is None else user_home
    effective_platform = sys.platform if platform is None else platform
    effective_environ = os.environ if environ is None else environ
    effective_tz = _resolve_display_tz(display_tz, effective_environ)

    cycles = 0
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
            return

        now = datetime.now(tz=UTC)
        try:
            refresh(
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
            emitted = _emit_cycle(collector_id, "failed", type(exc).__name__, cycle=cycles)
        else:
            emitted = _emit_cycle(collector_id, "ok", "", cycle=cycles)

        if not emitted and not unbound_reported:
            unbound_reported = True
            _log.warning(
                "collector {} cycle events cannot be emitted: no telemetry pipeline is bound in "
                "this process (a schedule runner binds none), so every event this loop produces "
                "is dropped. The loop's liveness is readable from plugin_stats.updated_at.",
                collector_id,
            )

        cycles += 1
        if stop_after_cycles is not None and cycles >= stop_after_cycles:
            return

        interval_s = float(collector.interval_minutes * 60)
        sleep(interval_s)


def run_from_schedule(borrowed: ScheduleInputs | None, *, collector_id: str) -> None:
    # meiva:note
    # The entry a generated schedule script calls. It supplies the loop's two effects; a test that
    # needs to control them calls `run_collector_cycles` with its own, which is why they are
    # required parameters there instead of defaults here (diff-hints callable-default).
    run_collector_cycles(
        borrowed, refresh=refresh_collector_once, sleep=time.sleep, collector_id=collector_id
    )
