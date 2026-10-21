# meiva:note
# Archive-to-provider source synchronization. The archive is the reviewed ledger; a built-in
# provider's ``PriceRates`` must carry the period, tier and window lattice the archive states
# for its model. ``plugin_price_source`` owns what a ``provider.py`` declares; this module
# owns how the archive reconciles it and how a rewrite lands back in the source.
# Synchronization preserves the complete lattice: effective periods, input-token tiers and
# recurring UTC windows. The flat ``PriceRates`` fields remain the base tier covering the
# synchronization instant.
# meiva:link base/lm/pricing/docs/pricing.meiva.okf.md

from __future__ import annotations

import ast
import json
import sys
from datetime import UTC, datetime, time
from pathlib import Path
from typing import Any, NamedTuple, cast

from scripts.model_registry.plugin_price_rates import (
    FlatRates,
    archive_rates,
    cache_write_lines,
)
from scripts.model_registry.plugin_price_source import (
    PeriodDeclaration,
    PriceDeclaration,
    ProviderManifest,
    TierDeclaration,
    WindowDeclaration,
    collapse_flat_periods,
    imported_names,
    provider_manifest,
    validated_provenance,
)


class PluginSyncResult(NamedTuple):
    """Observed drift, differing periods, and files changed by one pass."""

    drifted_models: tuple[str, ...]
    changed_files: tuple[Path, ...]
    drifted_periods: tuple[str, ...]


def _instant(value: str | None, *, context: str) -> datetime | None:
    if value is None:
        return None
    try:
        instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise RuntimeError(f"{context} must be an ISO-8601 instant") from exc
    if instant.utcoffset() is None:
        raise RuntimeError(f"{context} must carry a UTC offset")
    return instant.astimezone(UTC)


def _clock(value: object, *, context: str) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{context} must be an HH:MM:SS string")
    try:
        parsed = time.fromisoformat(value)
    except ValueError as exc:
        raise RuntimeError(f"{context} must be an HH:MM:SS string") from exc
    if parsed.tzinfo is not None:
        raise RuntimeError(f"{context} must be offset-free UTC wall time")
    return value


def _archive_windows(windows_raw: object, *, tier_context: str) -> tuple[WindowDeclaration, ...]:
    if not isinstance(windows_raw, list):
        raise TypeError(f"{tier_context} utc_daily_overrides must be an array")
    windows = tuple(
        WindowDeclaration(
            start=_clock(window["start"], context=f"{tier_context} window {index} start"),
            end=_clock(window["end"], context=f"{tier_context} window {index} end"),
            rates=archive_rates(window["rates"], context=f"{tier_context} window {index} rates"),
        )
        for index, window in enumerate(windows_raw)
        if isinstance(window, dict)
    )
    if len(windows) != len(windows_raw):
        raise TypeError(f"{tier_context} windows must be objects")
    return windows


def _archive_tier(
    model: str, tier_raw: object, *, tier_context: str, expected_min: int
) -> TierDeclaration:
    if not isinstance(tier_raw, dict):
        raise TypeError(f"{tier_context} must be an object")
    lower = tier_raw["input_tokens_min"]
    upper = tier_raw["input_tokens_max"]
    if isinstance(lower, bool) or not isinstance(lower, int):
        raise TypeError(f"{tier_context} input_tokens_min must be an integer")
    if upper is not None and (isinstance(upper, bool) or not isinstance(upper, int)):
        raise TypeError(f"{tier_context} input_tokens_max must be an integer or null")
    if lower != expected_min or (upper is not None and upper < lower):
        raise RuntimeError(f"archive {model!r} has a gap or overlap in tiers")
    windows = _archive_windows(tier_raw["utc_daily_overrides"], tier_context=tier_context)
    return TierDeclaration(
        input_tokens_min=lower,
        input_tokens_max=upper,
        rates=archive_rates(tier_raw["rates"], context=f"{tier_context} rates"),
        windows=windows,
    )


def _archive_tiers(model: str, tiers_raw: object, *, context: str) -> tuple[TierDeclaration, ...]:
    if not isinstance(tiers_raw, list) or not tiers_raw:
        raise RuntimeError(f"{context} must contain tiers")
    tiers: list[TierDeclaration] = []
    expected_min = 0
    for tier_index, tier_raw in enumerate(tiers_raw):
        tier = _archive_tier(
            model, tier_raw, tier_context=f"{context} tier {tier_index}", expected_min=expected_min
        )
        tiers.append(tier)
        expected_min = -1 if tier.input_tokens_max is None else tier.input_tokens_max + 1
    if expected_min != -1:
        raise RuntimeError(f"archive {model!r} tiers must cover all input sizes")
    return tuple(tiers)


def _archive_periods(model: str, raw: object) -> tuple[PeriodDeclaration, ...]:
    if not isinstance(raw, list) or not raw:
        raise RuntimeError(f"archive {model!r} must contain periods")
    periods: list[PeriodDeclaration] = []
    previous_until: str | None = None
    for period_index, period_raw in enumerate(raw):
        context = f"archive {model!r} period {period_index}"
        if not isinstance(period_raw, dict):
            raise TypeError(f"{context} must be an object")
        effective_from = cast(str | None, period_raw["effective_from"])
        effective_until = cast(str | None, period_raw["effective_until"])
        start = _instant(effective_from, context=f"{context} effective_from")
        end = _instant(effective_until, context=f"{context} effective_until")
        if period_index == 0 and start is not None:
            raise RuntimeError(f"archive {model!r} must cover all history")
        if period_index > 0 and effective_from != previous_until:
            raise RuntimeError(f"archive {model!r} has a gap or overlap in periods")
        if start is not None and end is not None and end <= start:
            raise RuntimeError(f"{context} is empty")
        tiers = _archive_tiers(model, period_raw["tiers"], context=context)
        periods.append(PeriodDeclaration(effective_from, effective_until, tiers))
        previous_until = effective_until
    if periods[-1].effective_until is not None:
        raise RuntimeError(f"archive {model!r} must cover the present")
    return tuple(periods)


def _base_rates(period: PeriodDeclaration, *, context: str) -> FlatRates:
    base = [tier.rates for tier in period.tiers if tier.input_tokens_min == 0]
    if len(base) != 1:
        raise RuntimeError(f"{context} must contain exactly one zero-input base tier")
    return base[0]


def archive_price(
    model: str,
    entry: dict[str, Any],
    now: datetime,
) -> tuple[PriceDeclaration, tuple[tuple[str, FlatRates], ...]]:
    periods = _archive_periods(model, entry["periods"])
    covering: list[PeriodDeclaration] = []
    upcoming: list[tuple[str, FlatRates]] = []
    for index, period in enumerate(periods):
        context = f"archive {model!r} period {index}"
        start = _instant(period.effective_from, context=f"{context} effective_from")
        end = _instant(period.effective_until, context=f"{context} effective_until")
        rates = _base_rates(period, context=context)
        if (start is None or now >= start) and (end is None or now < end):
            covering.append(period)
        if start is not None and start > now:
            upcoming.append((cast(str, period.effective_from), rates))
    if len(covering) != 1:
        raise RuntimeError(f"archive {model!r} must have exactly one period covering now")
    current = _base_rates(covering[0], context=f"archive {model!r} current period")
    source_url, checked_at, vendor = validated_provenance(
        entry["source_url"],
        entry["source_checked_at"],
        entry.get("vendor"),
        context=f"archive {model!r}",
    )
    return (
        PriceDeclaration(
            current, source_url, checked_at, vendor, collapse_flat_periods(periods, current)
        ),
        tuple(upcoming),
    )


def _render_window(window: WindowDeclaration, indent: int) -> list[str]:
    outer = " " * indent
    inner = " " * (indent + 4)
    return [
        f"{outer}PriceWindow(",
        f"{inner}start={json.dumps(window.start)},",
        f"{inner}end={json.dumps(window.end)},",
        f"{inner}cache_miss={json.dumps(str(window.rates.cache_miss))},",
        f"{inner}cache_hit={json.dumps(str(window.rates.cache_hit))},",
        f"{inner}output={json.dumps(str(window.rates.output))},",
        *cache_write_lines(window.rates, inner),
        f"{outer}),",
    ]


def _render_tier(tier: TierDeclaration, indent: int) -> list[str]:
    outer = " " * indent
    inner = " " * (indent + 4)
    lines = [
        f"{outer}PriceTier(",
        f"{inner}input_tokens_min={tier.input_tokens_min},",
        f"{inner}input_tokens_max={tier.input_tokens_max},",
        f"{inner}cache_miss={json.dumps(str(tier.rates.cache_miss))},",
        f"{inner}cache_hit={json.dumps(str(tier.rates.cache_hit))},",
        f"{inner}output={json.dumps(str(tier.rates.output))},",
        *cache_write_lines(tier.rates, inner),
    ]
    if tier.windows:
        lines.append(f"{inner}windows=(")
        for window in tier.windows:
            lines.extend(_render_window(window, indent + 8))
        lines.append(f"{inner}),")
    lines.append(f"{outer}),")
    return lines


def _string_or_none_literal(value: str | None) -> str:
    return "None" if value is None else json.dumps(value)


def _render_period(period: PeriodDeclaration, indent: int) -> list[str]:
    outer = " " * indent
    inner = " " * (indent + 4)
    lines = [
        f"{outer}PricePeriod(",
        f"{inner}effective_from={_string_or_none_literal(period.effective_from)},",
        f"{inner}effective_until={_string_or_none_literal(period.effective_until)},",
        f"{inner}tiers=(",
    ]
    for tier in period.tiers:
        lines.extend(_render_tier(tier, indent + 8))
    lines.extend((f"{inner}),", f"{outer}),"))
    return lines


def _render_price(price: PriceDeclaration, indent: int) -> str:
    outer = " " * indent
    inner = " " * (indent + 4)
    lines = [
        "PriceRates(",
        f"{inner}cache_miss={price.rates.cache_miss},",
        f"{inner}cache_hit={price.rates.cache_hit},",
        f"{inner}output={price.rates.output},",
        *cache_write_lines(price.rates, inner, quoted=False),
        f"{inner}source_url={json.dumps(price.source_url)},",
        f"{inner}source_checked_at={json.dumps(price.source_checked_at)},",
        f"{inner}vendor={_string_or_none_literal(price.vendor)},",
    ]
    if price.periods:
        lines.append(f"{inner}periods=(")
        for period in price.periods:
            lines.extend(_render_period(period, indent + 8))
        lines.append(f"{inner}),")
    lines.append(f"{outer})")
    return "\n".join(lines)


def _call_offsets(source: str, node: ast.Call, *, path: Path) -> tuple[int, int]:
    if node.end_lineno is None or node.end_col_offset is None:
        raise RuntimeError(f"{path}:{node.lineno}: PriceRates span is unavailable")
    lines = source.splitlines(keepends=True)
    start = sum(len(line) for line in lines[: node.lineno - 1]) + node.col_offset
    end = sum(len(line) for line in lines[: node.end_lineno - 1]) + node.end_col_offset
    if not source[start:end].startswith("PriceRates("):
        raise RuntimeError(f"{path}:{node.lineno}: PriceRates span is not line-anchored")
    return start, end


def _entry_indent(source: str, node: ast.Call) -> int:
    line = source.splitlines()[node.lineno - 1]
    return len(line) - len(line.lstrip(" "))


def _period_label(period: PeriodDeclaration, index: int) -> str:
    return f"period {index} [{period.effective_from!r}, {period.effective_until!r})"


def _drift_labels(
    model: str, declared: PriceDeclaration, target: PriceDeclaration
) -> tuple[str, ...]:
    labels: list[str] = []
    if declared.rates != target.rates:
        labels.append(f"{model}: current flat rate")
    if declared.source_checked_at != target.source_checked_at:
        labels.append(f"{model}: source_checked_at")
    if declared.periods != target.periods:
        for index in range(max(len(declared.periods), len(target.periods))):
            if index >= len(target.periods):
                labels.append(f"{model}: extra declared period {index}")
            elif index >= len(declared.periods) or declared.periods[index] != target.periods[index]:
                labels.append(f"{model}: {_period_label(target.periods[index], index)}")
    return tuple(labels)


def _rendered_calls(rendered: str) -> set[str]:
    expression = ast.parse(f"_price = {rendered}\n", mode="exec")
    names: set[str] = set()
    for node in ast.walk(expression):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            names.add(node.func.id)
    return names


def rewrite_provider(
    source: str,
    path: Path,
    manifest: ProviderManifest,
    targets: dict[str, PriceDeclaration],
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    replacements: list[tuple[int, int, str]] = []
    drifted: list[str] = []
    drifted_periods: list[str] = []
    for model, declared in manifest.prices.items():
        target = targets[model]
        if (declared.source_url, declared.vendor) != (target.source_url, target.vendor):
            raise RuntimeError(f"{path}: {model!r} source_url/vendor drift requires human review")
        labels = _drift_labels(model, declared, target)
        if not labels:
            continue
        drifted.append(model)
        drifted_periods.extend(labels)
        node = manifest.price_nodes[model]
        start, end = _call_offsets(source, node, path=path)
        replacements.append((start, end, _render_price(target, _entry_indent(source, node))))
    if replacements:
        imported = imported_names(source, path)
        rendered: set[str] = set()
        for _start, _end, text in replacements:
            rendered |= _rendered_calls(text)
        missing = sorted(rendered - imported)
        if missing:
            raise RuntimeError(
                f"{path}: rendering the archive lattice calls {missing}, which this module "
                f"does not import from the provider contract; add {missing} to its import "
                "block and re-run"
            )
    rewritten = source
    for start, end, replacement in sorted(replacements, reverse=True):
        rewritten = rewritten[:start] + replacement + rewritten[end:]
    round_trip = provider_manifest(rewritten, path)
    if round_trip.models != manifest.models or round_trip.prices.keys() != manifest.prices.keys():
        raise RuntimeError(f"{path}: rewrite changed the registered model or pricing set")
    for model, target in targets.items():
        if round_trip.prices[model] != target:
            raise RuntimeError(f"{path}: {model!r} did not round-trip to the archive price")
    ast.parse(rewritten, filename=str(path))
    return rewritten, tuple(drifted), tuple(drifted_periods)


def _regular_file(path: Path, root: Path, *, label: str) -> Path:
    resolved = path.resolve(strict=True)
    if path.is_symlink() or resolved != path.absolute() or not resolved.is_relative_to(root):
        raise RuntimeError(f"{label} must be a regular file inside {root}")
    if not resolved.is_file():
        raise RuntimeError(f"{label} must be a regular file")
    return resolved


def _reject_duplicate_prices(seen: set[str], manifest: ProviderManifest, *, path: Path) -> None:
    duplicate = seen & manifest.prices.keys()
    if duplicate:
        raise RuntimeError(f"{path}: plugin prices registered more than once: {sorted(duplicate)}")


def _plugin_targets(
    manifest: ProviderManifest,
    archive_models: dict[str, dict[str, Any]],
    *,
    path: Path,
    now: datetime,
) -> tuple[dict[str, PriceDeclaration], list[tuple[str, str, FlatRates]]]:
    targets: dict[str, PriceDeclaration] = {}
    notes: list[tuple[str, str, FlatRates]] = []
    for model in manifest.prices:
        if model not in archive_models:
            raise RuntimeError(f"{path}: archive has no entry for plugin-priced model {model!r}")
        target, upcoming = archive_price(model, archive_models[model], now)
        targets[model] = target
        notes.extend(
            (model, effective_from, rates)
            for effective_from, rates in upcoming
            if rates != target.rates
        )
    return targets, notes


# meiva:note
# Synchronize every built-in provider's complete price lattice. One failing provider fails the
# pass and writes nothing: plugin prices shadow the archive entry of the same model, so a
# half-synchronized tree would bill from a stale declaration while the diff looks complete.
# The loop still reports every provider, so one run names everything that is wrong instead of
# one file per run.
def sync_plugin_rates(
    archive_path: Path,
    repo_root: Path,
    *,
    now: datetime,
    write: bool = True,
) -> PluginSyncResult:
    root = repo_root.resolve(strict=True)
    archive = _regular_file(archive_path, root, label="pricing archive")
    raw = cast(dict[str, Any], json.loads(archive.read_text(encoding="utf-8")))
    archive_models = cast(dict[str, dict[str, Any]], raw["models"])
    provider_paths = sorted((root / "meiva_builtins/plugins").glob("lm_*/provider.py"))
    if not provider_paths:
        raise RuntimeError(f"no built-in provider.py files found under {root}")

    plans: list[tuple[Path, str]] = []
    drifted_models: list[str] = []
    drifted_periods: list[str] = []
    future_notes: list[tuple[str, str, FlatRates]] = []
    seen_models: set[str] = set()
    failures: list[str] = []
    for candidate in provider_paths:
        path = _regular_file(candidate, root, label="provider source")
        source = path.read_text(encoding="utf-8")
        try:
            manifest = provider_manifest(source, path)
            _reject_duplicate_prices(seen_models, manifest, path=path)
            targets, notes = _plugin_targets(manifest, archive_models, path=path, now=now)
            rewritten, drifted, differing_periods = rewrite_provider(
                source, path, manifest, targets
            )
        except (RuntimeError, TypeError) as exc:
            failures.append(str(exc))
            print(f"- NOT synchronized: {exc}", file=sys.stderr)
            continue
        seen_models.update(manifest.prices)
        future_notes.extend(notes)
        drifted_models.extend(drifted)
        drifted_periods.extend(differing_periods)
        if drifted:
            plans.append((path, rewritten))
        print(
            f"- {path.relative_to(root)}: {', '.join(manifest.declarations)} — "
            f"{len(manifest.prices)} priced model(s), "
            + (f"{len(drifted)} drifted" if drifted else "matches the archive")
        )

    if failures:
        raise RuntimeError(
            f"{len(failures)} of {len(provider_paths)} provider file(s) could not be "
            "synchronized, so no plugin rate was written:\n"
            + "\n".join(f"- {failure}" for failure in failures)
        )

    for detail in sorted(drifted_periods):
        print(f"- Plugin pricing drift: {detail}.", file=sys.stderr)
    for model, effective_from, rates in sorted(future_notes):
        print(
            f"- `{model}` changes at `{effective_from}` to cache miss {rates.cache_miss}, "
            f"cache hit {rates.cache_hit}, output {rates.output} USD/1M tokens.",
            file=sys.stderr,
        )
    changed_files: tuple[Path, ...] = ()
    if write:
        for path, rewritten in plans:
            path.write_text(rewritten, encoding="utf-8")
        changed_files = tuple(path for path, _rewritten in plans)
    return PluginSyncResult(
        tuple(sorted(drifted_models)),
        changed_files,
        tuple(sorted(drifted_periods)),
    )
