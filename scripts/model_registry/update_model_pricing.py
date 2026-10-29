#!/usr/bin/env python3
# meiva:note
# Fetch official DeepSeek pricing and reconcile it with Meiva's archive. Provider adapters are
# intentionally strict and independent: a changed page shape, missing model, unknown meter,
# unknown column annotation, unknown model identity, or unit invariant is an error, because
# automation must never turn an upstream parsing mistake into a price used for billing.
# meiva:note
# The page prices four catalog entries through two columns: the `deepseek-flash` column prices
# the canonical `deepseek-flash` id plus the retired deepseek-v4-flash and
# deepseek-v4-flash-vision-exp names (DeepSeek keeps accepting and billing them at the Flash
# price), and `deepseek-v4-pro` is a live column at its own rates.
# meiva:note
# A column's footnote marker and its MODEL VERSION cell are compared with the reviewed table and
# never read as a billing rule. DeepSeek printed a V4 Pro retirement footnote on 2026-09-11 and
# rewrote it the same day into "we have decided to continue providing API services for DeepSeek
# V4 Pro", without moving a single price; a comparison of printed prices alone cannot see that,
# and a rule inferred from the first wording goes on billing a retirement the vendor withdrew.
# meiva:note
# Peak windows are the page's daily UTC hours; the page scopes them Monday through Friday, so
# this ledger bills weekend hours at peak rates — a known overestimate the archive's
# daily-recurrence windows cannot express yet.
# meiva:link base/lm/pricing/docs/pricing.meiva.okf.md

from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, NamedTuple

import requests
from bs4 import BeautifulSoup

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from scripts.model_registry.plugin_price_sync import PluginSyncResult
from scripts.model_registry.plugin_price_sync import sync_plugin_rates as _sync_plugin_rates


class Rates(NamedTuple):
    """USD per one million input, cache-read, and output tokens."""

    input: Decimal
    cache_read: Decimal
    output: Decimal


class DeepSeekPrices(NamedTuple):
    """The two daily rate bands published for one DeepSeek model."""

    peak: Rates
    off_peak: Rates


# meiva:note
# One column's own label marker and MODEL VERSION cell, exactly as printed.
class ColumnAnnotation(NamedTuple):
    note: str | None
    version: str


# meiva:note
# Everything one page fetch yields: the rates per model, the recurring peak windows, and each
# column's annotation.
class DeepSeekCatalog(NamedTuple):
    models: dict[str, DeepSeekPrices]
    peak_windows: tuple[tuple[str, str], ...]
    annotations: dict[str, ColumnAnnotation]


_METERS = {
    "1M INPUT TOKENS (CACHE MISS)": "input",
    "1M INPUT TOKENS (CACHE HIT)": "cache_read",
    "1M OUTPUT TOKENS": "output",
}


# meiva:note
# One MODEL-row column: the catalog entries it prices and the annotation the review recorded.
class Column(NamedTuple):
    models: tuple[str, ...]
    note: str | None
    version: str


# meiva:note
# Official page columns → the catalog entries each column prices plus what the
# reviewed page said about that column. Since the 2026-09-17 rename (task
# #3750) four entries are priced through two columns: the flash column prices
# the canonical deepseek-flash id, and footnote (1) keeps the retired
# deepseek-v4-flash and deepseek-v4-flash-vision-exp names accepted and billed
# at the Flash price. `deepseek-v4-pro` carries no footnote and is a live
# column: DeepSeek withdrew its 2026-09-11 retirement the same day, so the
# catalogue bills the pro band the page kept printing. Both the label's
# footnote marker and the MODEL VERSION cell are compared below, so a column
# whose annotation moves while its prices stand still reaches a reviewer.
_DEEPSEEK_COLUMNS: dict[str, Column] = {
    "deepseek-flash": Column(
        models=(
            "deepseek-flash",
            "deepseek-v4-flash",
            "deepseek-v4-flash-vision-exp",
        ),
        note="1",
        version="DeepSeek-V4.1-Flash-1017",
    ),
    "deepseek-v4-pro": Column(
        models=("deepseek-v4-pro",),
        note=None,
        version="DeepSeek-V4-Pro-0813",
    ),
}
_PEAK_HOURS = re.compile(
    r"\bPeak hours are\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s+"
    r"and\s+(\d{2}:\d{2})\s*-\s*(\d{2}:\d{2})\s+UTC\b"
)
_USD = re.compile(r"\$(?:0|[1-9]\d*)(?:\.\d+)?")
_MODEL_LABEL_FOOTNOTE = re.compile(r"\s*\((\d+)\)$")
_DEEPSEEK_PRICING_URL = "https://api-docs.deepseek.com/quick_start/pricing/"
_CATALOG_PATH = Path(__file__).resolve().parents[2] / "base/lm/pricing/pricing_catalog_archive.json"
_REPO_ROOT = Path(__file__).resolve().parents[2]


def _usd(cell: str) -> Decimal:
    normalized = cell.strip()
    if _USD.fullmatch(normalized) is None:
        raise ValueError(f"invalid USD price: {cell!r}")
    try:
        value = Decimal(normalized[1:])
    except InvalidOperation as exc:
        raise ValueError(f"invalid USD price: {cell!r}") from exc
    if not value.is_finite() or value <= 0:
        raise ValueError(f"price must be positive: {cell!r}")
    return value


def _peak_windows(page_text: str) -> tuple[tuple[str, str], ...]:
    peak_hour_matches = _PEAK_HOURS.findall(page_text)
    if len(peak_hour_matches) != 1:
        raise ValueError("DeepSeek must publish exactly one recognized peak-hour statement")
    clocks = peak_hour_matches[0]
    return tuple((f"{clocks[index]}:00", f"{clocks[index + 1]}:00") for index in range(0, 4, 2))


# meiva:note
# Each MODEL column's id, mapped to the footnote marker its label carries, if any.
def _column_labels(rows: list[list[str]]) -> dict[str, str | None]:
    model_row = next((row for row in rows if row and row[0] == "MODEL"), None)
    if model_row is None or len(model_row) < 3:
        raise ValueError("DeepSeek MODEL row is missing or incomplete")
    labels: dict[str, str | None] = {}
    for cell in model_row[1:]:
        marker = _MODEL_LABEL_FOOTNOTE.search(cell)
        model = _MODEL_LABEL_FOOTNOTE.sub("", cell)
        if not model:
            raise ValueError("DeepSeek MODEL row contains an empty model id")
        if model in labels:
            raise ValueError("DeepSeek MODEL row contains duplicate model ids")
        labels[model] = marker.group(1) if marker is not None else None
    return labels


# meiva:note
# Each MODEL VERSION cell, keyed by the MODEL column it belongs to.
def _model_identities(rows: list[list[str]], labels: dict[str, str | None]) -> dict[str, str]:
    version_row = next((row for row in rows if row and row[0] == "MODEL VERSION"), None)
    if version_row is None or len(version_row) != len(labels) + 1:
        raise ValueError("DeepSeek MODEL VERSION row is missing or does not match the MODEL row")
    versions = [cell.strip() for cell in version_row[1:]]
    if not all(versions):
        raise ValueError("DeepSeek MODEL VERSION row contains an empty model identity")
    return dict(zip(labels, versions, strict=True))


def _meter_cells(
    rows: list[list[str]], index: int, model_count: int
) -> tuple[str, list[str], list[str]] | None:
    """The meter label plus off-peak and peak price cells of a meter row, or None for other rows."""
    row = rows[index]
    known_meters = [label for label in _METERS if label in row]
    token_cells = [cell for cell in row if "TOKEN" in cell.upper()]
    if token_cells and len(known_meters) != 1:
        raise ValueError(f"unknown DeepSeek pricing meter: {token_cells!r}")
    if not known_meters:
        return None
    meter_label = known_meters[0]
    if "OFF-PEAK" not in row:
        raise ValueError(f"{meter_label} row has no OFF-PEAK band")
    if index + 1 >= len(rows) or not rows[index + 1] or rows[index + 1][0] != "PEAK":
        raise ValueError(f"{meter_label} row has no following PEAK band")
    off_peak_cells = row[-model_count:]
    peak_cells = rows[index + 1][-model_count:]
    if len(off_peak_cells) != model_count or len(peak_cells) != model_count:
        raise ValueError(f"{meter_label} price coverage does not match MODEL row")
    return meter_label, off_peak_cells, peak_cells


def _read_meter_rows(
    rows: list[list[str]], models: list[str]
) -> dict[str, dict[str, dict[str, Decimal]]]:
    """Collect per-model peak/off-peak rates from each meter row and its following PEAK row."""
    bands: dict[str, dict[str, dict[str, Decimal]]] = {
        model: {"peak": {}, "off_peak": {}} for model in models
    }
    for index in range(len(rows)):
        cells = _meter_cells(rows, index, len(models))
        if cells is None:
            continue
        meter_label, off_peak_cells, peak_cells = cells
        meter = _METERS[meter_label]
        for model, off_peak_cell, peak_cell in zip(models, off_peak_cells, peak_cells, strict=True):
            if meter in bands[model]["off_peak"] or meter in bands[model]["peak"]:
                raise ValueError(f"DeepSeek {model} contains duplicate {meter.upper()} pricing")
            bands[model]["off_peak"][meter] = _usd(off_peak_cell)
            bands[model]["peak"][meter] = _usd(peak_cell)
    return bands


def _model_prices(model: str, model_bands: dict[str, dict[str, Decimal]]) -> DeepSeekPrices:
    for band_name in ("peak", "off_peak"):
        missing = set(Rates._fields) - model_bands[band_name].keys()
        if missing:
            labels = ", ".join(sorted(name.upper() for name in missing))
            raise ValueError(f"DeepSeek {model} is missing {labels} pricing")
    peak = Rates(**model_bands["peak"])
    off_peak = Rates(**model_bands["off_peak"])
    if any(off * 2 != on for off, on in zip(off_peak, peak, strict=True)):
        raise ValueError(f"DeepSeek {model} off-peak prices are not half of peak prices")
    return DeepSeekPrices(peak=peak, off_peak=off_peak)


# meiva:note
# The table uses rowspans, so a peak row inherits the meter named by the preceding off-peak row,
# and column labels carry footnote markers (`deepseek-flash (1)`) that are stripped to the model
# id and reported beside it, never read as a rule. All three meters and the documented 50%
# off-peak relationship are required before any value is returned.
def parse_deepseek_pricing(html: str) -> DeepSeekCatalog:
    soup = BeautifulSoup(html, "lxml")
    peak_windows = _peak_windows(soup.get_text(" ", strip=True))
    rows = [
        [cell.get_text(" ", strip=True) for cell in row.select("th,td")]
        for row in soup.select("table tr")
    ]
    labels = _column_labels(rows)
    versions = _model_identities(rows, labels)
    bands = _read_meter_rows(rows, list(labels))
    parsed = {model: _model_prices(model, model_bands) for model, model_bands in bands.items()}
    annotations = {
        model: ColumnAnnotation(note=note, version=versions[model])
        for model, note in labels.items()
    }
    return DeepSeekCatalog(
        models=parsed,
        peak_windows=peak_windows,
        annotations=annotations,
    )


def _catalog_rates(raw: dict[str, str]) -> Rates:
    try:
        return Rates(
            input=Decimal(raw["input"]),
            cache_read=Decimal(raw["cache_read"]),
            output=Decimal(raw["output"]),
        )
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("catalog rates must contain decimal input/cache_read/output") from exc


def _period_deepseek_prices(
    period: dict[str, Any],
) -> tuple[DeepSeekPrices, tuple[tuple[str, str], ...]]:
    """Read one catalog period back into fetched-shape prices and windows."""
    tiers = period["tiers"]
    if (
        len(tiers) != 1
        or tiers[0]["input_tokens_min"] != 0
        or tiers[0]["input_tokens_max"] is not None
    ):
        raise ValueError("DeepSeek pricing must have one unbounded token tier")
    tier = tiers[0]
    overrides = tier["utc_daily_overrides"]
    windows = tuple((override["start"], override["end"]) for override in overrides)
    if not windows:
        raise ValueError("DeepSeek pricing must carry UTC peak windows")
    peak_rates = {_catalog_rates(override["rates"]) for override in overrides}
    if len(peak_rates) != 1:
        raise ValueError("DeepSeek UTC peak windows disagree on rates")
    prices = DeepSeekPrices(peak=peak_rates.pop(), off_peak=_catalog_rates(tier["rates"]))
    return prices, windows


def _current_deepseek_prices(
    entry: dict[str, Any],
) -> tuple[DeepSeekPrices, tuple[tuple[str, str], ...]]:
    current = [period for period in entry["periods"] if period["effective_until"] is None]
    if len(current) != 1:
        raise ValueError("DeepSeek catalog entry must have exactly one current period")
    return _period_deepseek_prices(current[0])


def _rates_json(rates: Rates) -> dict[str, str]:
    return {
        "input": str(rates.input),
        "cache_read": str(rates.cache_read),
        "output": str(rates.output),
    }


def _new_deepseek_period(
    prices: DeepSeekPrices,
    effective_from: str,
    peak_windows: tuple[tuple[str, str], ...],
) -> dict[str, Any]:
    return {
        "effective_from": effective_from,
        "effective_until": None,
        "tiers": [
            {
                "input_tokens_min": 0,
                "input_tokens_max": None,
                "rates": _rates_json(prices.off_peak),
                "utc_daily_overrides": [
                    {"start": start, "end": end, "rates": _rates_json(prices.peak)}
                    for start, end in peak_windows
                ],
            }
        ],
    }


def _append_reviewed_period(
    entry: dict[str, Any],
    prices: DeepSeekPrices,
    peak_windows: tuple[tuple[str, str], ...],
    detected_at: str,
) -> bool:
    """Append fetched rates as a new period when the open one differs."""
    current_prices, current_windows = _current_deepseek_prices(entry)
    if current_prices == prices and current_windows == peak_windows:
        return False
    current = next(period for period in entry["periods"] if period["effective_until"] is None)
    current["effective_until"] = detected_at
    entry["periods"].append(_new_deepseek_period(prices, detected_at, peak_windows))
    entry["source_checked_at"] = detected_at[:10]
    return True


# meiva:note
# Fail closed unless the page annotates every column exactly as the review recorded. A footnote
# marker on a column's label, or a changed MODEL VERSION cell, says the vendor is talking about
# that column's meaning while the printed prices may stay identical; the adapter interprets
# neither, so a marker it was not told about and an identity the catalog was not reviewed against
# are review items.
def _require_reviewed_annotations(fetched: DeepSeekCatalog) -> None:
    for column, reviewed in sorted(_DEEPSEEK_COLUMNS.items()):
        found = fetched.annotations[column]
        if (found.note, found.version) == (reviewed.note, reviewed.version):
            continue
        raise ValueError(
            f"DeepSeek {column!r} is published as {found.version!r} with footnote marker "
            f"{found.note!r}; the catalog was reviewed against {reviewed.version!r} with "
            f"marker {reviewed.note!r}. The adapter never reads a billing rule out of a "
            "footnote — review the page's wording before the catalog records one."
        )


# meiva:note
# Return a catalog copy with changed rates appended, or None if equal. The page's per-column
# annotations are checked first: a column whose footnote marker or model identity differs from
# the reviewed table fails closed, because a vendor changes what a column bills without moving
# the numbers it prints. `detected_at` is the provisional effective time: the generated PR must
# verify it against the provider announcement before merge, and retaining the prior period makes
# that correction a one-field edit instead of rewriting price history.
def reconcile_deepseek_catalog(
    catalog: dict[str, Any],
    fetched: DeepSeekCatalog,
    *,
    detected_at: str,
) -> dict[str, Any] | None:
    instant = datetime.fromisoformat(detected_at.replace("Z", "+00:00"))
    if instant.tzinfo is None or instant.utcoffset() is None or not detected_at.endswith("Z"):
        raise ValueError("detected_at must be an ISO-8601 UTC instant ending in Z")
    if set(fetched.models) != set(_DEEPSEEK_COLUMNS):
        raise ValueError(f"DeepSeek source must contain exactly {sorted(_DEEPSEEK_COLUMNS)}")
    _require_reviewed_annotations(fetched)

    updated = deepcopy(catalog)
    changed = False
    for column, reviewed in sorted(_DEEPSEEK_COLUMNS.items()):
        for model in reviewed.models:
            entry = updated["models"][model]
            appended = _append_reviewed_period(
                entry, fetched.models[column], fetched.peak_windows, detected_at
            )
            changed = changed or appended

    if not changed:
        return None
    updated["catalog_version"] = detected_at
    return updated


def _detected_at(now: datetime) -> str:
    return now.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def sync_plugin_rates(
    archive_path: Path,
    repo_root: Path,
    *,
    write: bool = True,
    now: datetime | None = None,
) -> PluginSyncResult:
    """Synchronize built-in providers to the archive's complete price semantics."""
    return _sync_plugin_rates(
        archive_path,
        repo_root,
        now=datetime.now(UTC) if now is None else now,
        write=write,
    )


def _fetch_deepseek_html() -> str:
    response = requests.get(
        _DEEPSEEK_PRICING_URL,
        headers={"User-Agent": "Meiva model-pricing updater"},
        timeout=30,
    )
    response.raise_for_status()
    return response.content.decode("utf-8")


def _run_sync_plugins(
    catalog_path: Path, repo_root: Path, *, write: bool, current_now: datetime
) -> int:
    result = sync_plugin_rates(catalog_path, repo_root, write=write, now=current_now)
    if result.drifted_models and not write:
        print(f"Plugin pricing drift detected for: {', '.join(result.drifted_models)}")
        return 1
    if result.changed_files:
        print(f"Synchronized plugin rates in {len(result.changed_files)} provider file(s).")
    else:
        print("Plugin prices match the archive's full period/tier/window semantics.")
    return 0


def main(argv: list[str] | None = None, *, now: datetime | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Compare Meiva's reviewed catalog with official provider pricing."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--write",
        action="store_true",
        help="append detected changes to the catalog for a reviewable PR",
    )
    mode.add_argument(
        "--check",
        action="store_true",
        help="report drift without writing and exit 1 when changes are needed",
    )
    parser.add_argument(
        "--sync-plugins",
        action="store_true",
        help="synchronize plugin PriceRates from the archive instead of fetching DeepSeek",
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        help="read a captured DeepSeek HTML page instead of the live official URL",
    )
    parser.add_argument("--catalog", type=Path)
    parser.add_argument(
        "--repo-root",
        type=Path,
        help="repository whose built-in provider files are synchronized",
    )
    parser.add_argument(
        "--detected-at",
        default=None,
        help="provisional UTC effective instant (default: current time)",
    )
    args = parser.parse_args(argv)

    current_now = datetime.now(UTC) if now is None else now
    repo_root = args.repo_root or _REPO_ROOT
    catalog_path = args.catalog or (
        repo_root / "base/lm/pricing/pricing_catalog_archive.json"
        if args.sync_plugins
        else _CATALOG_PATH
    )
    if args.sync_plugins:
        if args.source_file is not None or args.detected_at is not None:
            parser.error("--source-file/--detected-at cannot be used with --sync-plugins")
        return _run_sync_plugins(catalog_path, repo_root, write=args.write, current_now=current_now)

    html = (
        args.source_file.read_text(encoding="utf-8")
        if args.source_file is not None
        else _fetch_deepseek_html()
    )
    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    detected_at = args.detected_at or _detected_at(current_now)
    updated = reconcile_deepseek_catalog(
        catalog,
        parse_deepseek_pricing(html),
        detected_at=detected_at,
    )
    if updated is None:
        print("DeepSeek pricing matches the reviewed catalog.")
        return 0
    if not args.write:
        print(
            "DeepSeek pricing drift detected. Run with --write to append a provisional "
            "effective period, then verify its timestamp against the provider announcement."
        )
        return 1
    catalog_path.write_text(
        json.dumps(updated, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    print(f"Updated {catalog_path} at provisional effective time {detected_at}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
