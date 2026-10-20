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
  return (
    <div className={COLUMNS} data-testid="spawn-table">
      <div className="border-b border-border px-2 py-1.5 font-medium text-muted-foreground">{t("harness")}</div>
      <div className="border-b border-border px-2 py-1.5 font-medium text-muted-foreground">{t("model")}</div>
      <div className="border-b border-border px-2 py-1.5 font-medium text-muted-foreground">{t("reasoningEffort")}</div>

      <ul aria-label={t("harness")} className="max-h-[300px] overflow-y-auto border-r border-border py-1 pr-1">
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

      <ul aria-label={t("model")} className="max-h-[300px] overflow-y-auto border-r border-border py-1 pr-1">
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
                      className={cn(CELL, selected ? SELECTED : IDLE)}
                    >
                      <span className="truncate" title={candidate}>{candidate}</span>
                      {price !== undefined && (
                        <span className="ml-auto shrink-0 pl-2 text-[10px] tabular-nums text-muted-foreground">
                          {formatModelPrice(price)}
                        </span>
                      )}
                      {selected && <Check className="ml-auto size-3 shrink-0" />}
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

      <ul aria-label={t("reasoningEffort")} className="max-h-[300px] overflow-y-auto py-1">
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
