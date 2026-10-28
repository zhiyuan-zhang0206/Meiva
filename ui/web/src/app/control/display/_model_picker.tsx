"use client";

// meiva:note
// The model picker's list, two levels deep: a provider band and the models it
// carries. The band is a heading plus that family's master switch, set on a
// tinted strip; the models below it start at the band's own text edge, so
// membership is read from position instead of being re-stated in every row
// (user 2026-10-28: the provider and its models looked like siblings). A
// switched-off family dims as a whole: its rows keep their own state, which is
// what switching the provider back on restores. `role=group` + `aria-labelledby`
// on each family is what puts the provider in the accessibility tree above its
// models: without it the list is one flat run of switches named "Claude",
// "claude-opus-5-5", ... with no parent to either of them.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md

import { Switch } from "@/components/ui/switch";
import { providerLabel } from "@/lib/contracts/models";
import type { ModelsResponse } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, MIN_W_0 } from "@/lib/layout/layout";

const MODEL_ROW = "justify-between gap-4 py-1.5 pl-10 pr-3";

export interface ModelPickerProps {
  groups: [provider: string, models: string[]][];
  models: ModelsResponse["models"] | undefined;
  hiddenModels: readonly string[];
  hiddenProviders: readonly string[];
  onToggleModel: (model: string, visible: boolean) => void;
  onToggleProvider: (provider: string, visible: boolean) => void;
}

export function ModelPickerSection({
  groups,
  models,
  hiddenModels,
  hiddenProviders,
  onToggleModel,
  onToggleProvider,
}: ModelPickerProps) {
  return (
    <div data-testid="model-picker" className="divide-y divide-border [&>*+*]:pt-2">
      {groups.map(([provider, providerModels]) => {
        const providerOff = hiddenProviders.includes(provider);
        const label = providerLabel(provider);
        const headingId = `model-picker-provider-${provider}`;
        return (
          <div
            key={provider}
            role="group"
            aria-labelledby={headingId}
            data-provider-group={provider}
          >
            <div className={cn("items-center gap-4 bg-muted/60 px-3 py-2", FLEX)}>
              <div className={cn(MIN_W_0, FLEX_1)}>
                <h4
                  id={headingId}
                  className="text-[11px] font-semibold uppercase tracking-wide text-muted-foreground"
                >
                  {label}
                </h4>
                <p className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
                  All {providerModels.length} model{providerModels.length === 1 ? "" : "s"} come
                  from this provider — switch it off to take the family out of the spawn picker at
                  once
                </p>
              </div>
              <Switch
                checked={!providerOff}
                onCheckedChange={(on) => onToggleProvider(provider, on)}
                aria-label={label}
              />
            </div>
            <ul
              className={cn(
                "divide-y divide-border/60 border-t border-border",
                providerOff && "opacity-60",
              )}
            >
              {providerModels.map((modelName) => {
                const hidden = hiddenModels.includes(modelName);
                const price = models?.[modelName]?.pricing;
                return (
                  <li key={modelName} className={cn(MODEL_ROW, FLEX)}>
                    <div className={cn(MIN_W_0, FLEX_1)}>
                      <div className="truncate text-[13px]">{modelName}</div>
                      {price ? (
                        <div className="text-[11px] tabular-nums text-muted-foreground">
                          ${price.input.toFixed(2)} / ${price.cache_read.toFixed(2)} / $
                          {price.output.toFixed(2)}
                        </div>
                      ) : null}
                    </div>
                    <Switch
                      checked={!hidden}
                      disabled={providerOff}
                      onCheckedChange={(on) => onToggleModel(modelName, on)}
                      aria-label={modelName}
                    />
                  </li>
                );
              })}
            </ul>
          </div>
        );
      })}
    </div>
  );
}
