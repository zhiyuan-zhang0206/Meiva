"use client";

// meiva:note
// The spawn form is one table: harness, then the models that harness runs, then
// the efforts that model offers. Each column narrows the next, and the row the
// user last chose is marked on entry, so opening the form answers "what will the
// next agent be born on" before it asks anything (user 2026-10-20).
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md

import { Check } from "lucide-react";
import { useTranslations } from "next-intl";

import type { ModelsResponse } from "@/lib/contracts/types";
import { providerLabel } from "@/lib/contracts/models";
import { formatModelPrice } from "@/lib/format/format-price";
import { cn } from "@/lib/format/utils";
import { FLEX_1, MIN_W_0 } from "@/lib/layout/layout";
import {
  harnessLabel,
  type Harness,
  type HarnessAvailability,
} from "@/lib/harness/harness";

const COLUMNS = "grid grid-cols-[8.5rem_minmax(0,1fr)_5rem] text-xs";
const CELL = "flex w-full items-center gap-1 rounded-sm px-2 py-1 text-left";
const SELECTED = "bg-foreground/10 font-medium text-foreground";
const IDLE = "text-muted-foreground hover:bg-accent hover:text-foreground";

export interface SpawnTableProps {
  harnessRows: HarnessAvailability[];
  harness: Harness;
  onHarness: (harness: Harness) => void;
  modelGroups: [provider: string, models: string[]][];
  models: ModelsResponse["models"] | undefined;
  model: string | undefined;
  onModel: (model: string) => void;
  efforts: string[];
  effort: string | undefined;
  onEffort: (effort: string) => void;
}

export function SpawnTable({
  harnessRows,
  harness,
  onHarness,
  modelGroups,
  models,
  model,
  onModel,
  efforts,
  effort,
  onEffort,
}: SpawnTableProps) {
  const t = useTranslations("spawn");
  // meiva:note
  // The model cell keeps the input rate visible, because comparing models is what
  // the column is for; everything the old full-width row carried - every rate
  // behind that price, and the vendor TPS record with its source - is one hover
  // away. A 5rem column cannot hold four prices (user 2026-10-20).
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  function detailFor(candidate: string): string | undefined {
    const info = models?.[candidate];
    if (info === undefined) return undefined;
    const price = info.pricing;
    const tps = info.reference_tps;
    return [
      ...(price
        ? [
            t("priceBreakdown"),
            t("pricingConditions"),
            ...(price.cache_write_5m != null
              ? [t("cacheWritePrice", { duration: "5m", price: formatModelPrice(price.cache_write_5m) })]
              : []),
            ...(price.cache_write_1h != null
              ? [t("cacheWritePrice", { duration: "1h", price: formatModelPrice(price.cache_write_1h) })]
              : []),
          ]
        : []),
      tps
        ? `${t("referenceTps")}: ${tps.display} — ${tps.note} (${tps.source_url} · ${tps.source_checked_at})`
        : t("referenceTpsUnavailable"),
    ].join("\n");
  }
  return (
    <div className={COLUMNS} data-testid="spawn-table">
      <div className="border-b border-border px-2 py-1.5 font-medium text-muted-foreground">{t("harness")}</div>
      <div className="border-b border-border px-2 py-1.5 font-medium text-muted-foreground">{t("model")}</div>
      <div className="border-b border-border px-2 py-1.5 font-medium text-muted-foreground">{t("reasoningEffort")}</div>

      <ul aria-label={t("harness")} className="max-h-[300px] overflow-y-auto border-r border-border py-1 pr-1 pb-2">
        {harnessRows.map((row) => {
          const selected = row.harness === harness;
          return (
            <li key={row.harness}>
              <button
                type="button"
                disabled={!row.launchable}
                aria-pressed={selected}
                onClick={() => onHarness(row.harness)}
                className={cn(CELL, selected ? SELECTED : IDLE, "disabled:opacity-50 disabled:hover:bg-transparent")}
              >
                <span className="truncate">{harnessLabel(row.harness)}</span>
                {selected && <Check className="ml-auto size-3 shrink-0" />}
              </button>
            </li>
          );
        })}
      </ul>

      <ul aria-label={t("model")} className="max-h-[300px] overflow-y-auto border-r border-border py-1 pr-1 pb-2">
        {modelGroups.map(([provider, providerModels]) => (
          <li key={provider}>
            <div className="px-2 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70 first:pt-1">
              {providerLabel(provider)}
            </div>
            <ul>
              {providerModels.map((candidate) => {
                const selected = candidate === model;
                const price = models?.[candidate]?.pricing?.input;
                return (
                  <li key={candidate}>
                    <button
                      type="button"
                      aria-pressed={selected}
                      onClick={() => onModel(candidate)}
                      title={detailFor(candidate)}
                      className={cn(CELL, selected ? SELECTED : IDLE)}
                    >
                      <span className={cn(MIN_W_0, FLEX_1, "truncate")}>{candidate}</span>
                      {price !== undefined && (
                        <span className="shrink-0 pl-2 text-[10px] tabular-nums text-muted-foreground">
                          {formatModelPrice(price)}
                        </span>
                      )}
                      {selected && <Check className="ml-1 size-3 shrink-0" />}
                    </button>
                  </li>
                );
              })}
            </ul>
          </li>
        ))}
        {modelGroups.length === 0 && (
          <li className="px-2 py-1 text-muted-foreground">{t("modelsUnavailable")}</li>
        )}
      </ul>

      <ul aria-label={t("reasoningEffort")} className="max-h-[300px] overflow-y-auto py-1 pb-2">
        {efforts.map((level) => {
          const selected = level === effort;
          return (
            <li key={level}>
              <button
                type="button"
                aria-pressed={selected}
                onClick={() => onEffort(level)}
                className={cn(CELL, selected ? SELECTED : IDLE)}
              >
                <span className="truncate">{level}</span>
                {selected && <Check className="ml-auto size-3 shrink-0" />}
              </button>
            </li>
          );
        })}
        {efforts.length === 0 && (
          <li className="px-2 py-1 text-muted-foreground">—</li>
        )}
      </ul>
    </div>
  );
}
