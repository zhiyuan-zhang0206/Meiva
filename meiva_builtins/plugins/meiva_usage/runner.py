# meiva:note
# Schedule runner entrypoint for meiva_usage collectors.
# meiva:link meiva_builtins/plugins/meiva_usage/docs/meiva_usage.meiva.okf.md

from __future__ import annotations

import os
import sys
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime, tzinfo
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from base.daemon.schedules.inputs import ScheduleInputs
from base.db import Database
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
) -> CardReading:
    error_detail: str | None = None
    reading: CardReading
    try:
        reading = probe_collector(
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
        error_detail = err_msg
        reading = CardReading(value="—", detail=err_msg, status="error")

    if error_detail is not None:
        with db.write_transaction() as conn, conn.cursor() as cur:
            cur.execute(
                "SELECT status, updated_at, value FROM plugin_stats WHERE plugin = %s AND id = %s",
                ("meiva_usage", collector.id),
            )
            row = cur.fetchone()
            existing_status = str(row[0]) if row and row[0] is not None else None
            existing_updated_at = row[1] if row else None
            existing_value = str(row[2]) if row and row[2] is not None else None
            if not should_write_failure(existing_status, existing_updated_at, now):
                return CardReading(
                    value=existing_value or "—",
                    detail="probe error suppressed (cached reading fresh)",
                    status=existing_status if existing_status in ("ok", "warn", "error") else "ok",
                )
            if existing_value:
                reading = CardReading(value=existing_value, detail=error_detail, status="error")

    stats.upsert(
        db,
        plugin="meiva_usage",
        id=collector.id,
        value=reading.value,
        detail=reading.detail,
        status=reading.status,
        updated_by=machine_name,
        detail_cap=500,
    )
    return reading


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


def run_from_schedule(
    borrowed: ScheduleInputs | None,
    *,
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
    while True:
        collector = _load_collector_config(collector_id, home, effective_environ)
        if collector is None:
            return

        now = datetime.now(tz=UTC)
        try:
            refresh_collector_once(
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

        cycles += 1
        if stop_after_cycles is not None and cycles >= stop_after_cycles:
            return

        interval_s = float(collector.interval_minutes * 60)
        time.sleep(interval_s)
