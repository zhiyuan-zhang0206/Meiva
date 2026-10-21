"use client";

// meiva:note
// The spawn form is one table, read left to right: the machine, then the
// harnesses that machine can start, then the models that harness runs, then the
// efforts that model offers. Each column narrows the next, and the row the user
// last chose is marked on entry, so opening the form answers "what will the next
// agent be born on, and where" before it asks anything (user 2026-10-20: the
// machine comes first — a harness is only available *on* a machine).
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md

import { Check, ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";
import { useState } from "react";

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

const COLUMNS = "grid grid-cols-[7rem_7.5rem_minmax(0,1fr)_4.5rem] border-b border-border text-xs";
const HEADER = "border-b border-r border-border px-2 py-1.5 font-medium text-muted-foreground";
const CELL = "flex w-full items-center gap-1 rounded-sm px-2 py-1 text-left";
const SELECTED = "bg-foreground/10 font-medium text-foreground";
const IDLE = "text-muted-foreground hover:bg-accent hover:text-foreground";

// meiva:note
// One machine row: the host the agent would be born on, and why it cannot be
// picked when it cannot.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
export interface SpawnMachine {
  name: string;
  reason?: string;
}

export interface SpawnPickerProps {
  machines: SpawnMachine[];
  machine: string | undefined;
  onMachine: (machine: string) => void;
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
  // meiva:note
  // Whether this harness bills per token: a subscription pays nothing per token,
  // so its rates are noise beside the model name (user 2026-10-20).
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  prices: boolean;
}

export function SpawnTable({
  machines,
  machine,
  onMachine,
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
  prices,
}: SpawnPickerProps) {
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
      <div className={HEADER}>{t("machine")}</div>
      <div className={HEADER}>{t("harness")}</div>
      <div className={HEADER}>{t("model")}</div>
      <div className={cn(HEADER, "border-r-0")}>{t("reasoningEffort")}</div>

      <ul aria-label={t("machine")} className="max-h-[300px] overflow-y-auto border-r border-border py-1 pr-1 pb-2">
        {machines.map((row) => {
          const selected = row.name === machine;
          const blocked = row.reason !== undefined;
          return (
            <li key={row.name}>
              <button
                type="button"
                disabled={blocked}
                aria-pressed={selected}
                title={row.reason ?? row.name}
                onClick={() => onMachine(row.name)}
                className={cn(CELL, selected ? SELECTED : IDLE, "disabled:opacity-50 disabled:hover:bg-transparent")}
              >
                <span className={cn(MIN_W_0, FLEX_1, "truncate")}>{row.name}</span>
                {selected && <Check className="ml-1 size-3 shrink-0" />}
              </button>
            </li>
          );
        })}
      </ul>

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
                const price = models?.[candidate]?.pricing;
                return (
                  <li key={candidate}>
                    <button
                      type="button"
                      aria-pressed={selected}
                      onClick={() => onModel(candidate)}
                      title={detailFor(candidate)}
                      className={cn(CELL, selected ? SELECTED : IDLE)}
                    >
                      <span className={cn(MIN_W_0, FLEX_1)}>
                        <span className="block truncate">{candidate}</span>
                        {prices && price != null && (
                          <span className="block truncate text-[10px] tabular-nums text-muted-foreground">
                            {formatModelPrice(price.input)} / {formatModelPrice(price.cache_read)} /{" "}
                            {formatModelPrice(price.output)}
                          </span>
                        )}
                      </span>
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

// meiva:note
// Below md the four columns have nowhere to go: the popover is 94vw wide, the
// action rail keeps 88px of it, and on a 390px phone the model column measured
// 4px of usable width (2026-10-21) — the one column that decides the agent was
// the one that vanished. The same four choices render as stacked sections
// instead: a closed section states the current pick, tapping it opens that one
// list, and picking closes it again, so the form still answers "what will be
// born, and where" before it asks anything, and every row is a 44px touch
// target. The two arrangements share this file and the same props so a rule
// (availability, cascade, selection) can never exist in only one of them.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
const STACK_SECTION = "border-b border-border";
const STACK_CELL = "flex w-full min-h-11 items-center gap-2 px-3 py-2 text-left";
const STACK_LIST = "max-h-[40vh] overflow-y-auto border-t border-border py-1";
const STACK_NOTE = "block truncate text-[11px] text-muted-foreground";

type StackSectionKey = "machine" | "harness" | "model" | "effort";



function StackSection({
  id,
  label,
  value,
  note,
  expanded,
  onToggle,
  children,
}: {
  id: StackSectionKey;
  label: string;
  value: string;
  note?: string;
  expanded: boolean;
  onToggle: () => void;
  children: React.ReactNode;
}) {
  return (
    <div className={STACK_SECTION}>
      <button
        type="button"
        aria-expanded={expanded}
        aria-controls={expanded ? `spawn-stack-${id}` : undefined}
        onClick={onToggle}
        className={cn(STACK_CELL, "hover:bg-accent")}
      >
        <span className="shrink-0 text-xs text-muted-foreground">{label}</span>
        <span className={cn(MIN_W_0, FLEX_1)}>
          <span className="block truncate text-sm">{value}</span>
          {note !== undefined && <span className={STACK_NOTE}>{note}</span>}
        </span>
        <ChevronDown
          className={cn(
            "size-4 shrink-0 text-muted-foreground transition-transform",
            expanded && "rotate-180",
          )}
        />
      </button>
      {expanded ? <div id={`spawn-stack-${id}`}>{children}</div> : null}
    </div>
  );
}

function StackRow({
  selected,
  disabled,
  onClick,
  children,
}: {
  selected: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      aria-pressed={selected}
      onClick={onClick}
      className={cn(
        STACK_CELL,
        selected ? SELECTED : IDLE,
        "disabled:opacity-50 disabled:hover:bg-transparent",
      )}
    >
      {children}
      {selected && <Check className="ml-auto size-4 shrink-0" />}
    </button>
  );
}

export function SpawnStack({
  machines,
  machine,
  onMachine,
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
  prices,
}: SpawnPickerProps) {
  const t = useTranslations("spawn");
  // meiva:note
  // One open section at a time: the panel stays short enough to keep the action
  // visible, and picking a value closes the section it belongs to.
  // meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
  const [open, setOpen] = useState<StackSectionKey | null>(null);
  const toggle = (key: StackSectionKey) =>
    setOpen((current) => (current === key ? null : key));
  const pick = (key: StackSectionKey, apply: () => void) => {
    apply();
    setOpen(null);
  };

  const target = machines.find((row) => row.name === machine);
  const harnessBlocked =
    harnessRows.find((row) => row.harness === harness)?.launchable === false;

  return (
    <div data-testid="spawn-stack">
      <StackSection
        id="machine"
        label={t("machine")}
        value={machine ?? "—"}
        note={target?.reason}
        expanded={open === "machine"}
        onToggle={() => toggle("machine")}
      >
        <ul aria-label={t("machine")} className={STACK_LIST}>
          {machines.map((row) => {
            const selected = row.name === machine;
            return (
              <li key={row.name}>
                <StackRow
                  selected={selected}
                  disabled={row.reason !== undefined}
                  onClick={() => pick("machine", () => onMachine(row.name))}
                >
                  <span className={cn(MIN_W_0, FLEX_1)}>
                    <span className="block truncate">{row.name}</span>
                    {row.reason !== undefined && <span className={STACK_NOTE}>{row.reason}</span>}
                  </span>
                </StackRow>
              </li>
            );
          })}
        </ul>
      </StackSection>

      <StackSection
        id="harness"
        label={t("harness")}
        value={harnessLabel(harness)}
        note={harnessBlocked ? t("harnessStatus.unavailable") : undefined}
        expanded={open === "harness"}
        onToggle={() => toggle("harness")}
      >
        <ul aria-label={t("harness")} className={STACK_LIST}>
          {harnessRows.map((row) => {
            const selected = row.harness === harness;
            return (
              <li key={row.harness}>
                <StackRow
                  selected={selected}
                  disabled={!row.launchable}
                  onClick={() => pick("harness", () => onHarness(row.harness))}
                >
                  <span className={cn(MIN_W_0, FLEX_1)}>
                    <span className="block truncate">{harnessLabel(row.harness)}</span>
                    {!row.launchable && (
                      <span className={STACK_NOTE}>{t("harnessStatus.unavailable")}</span>
                    )}
                  </span>
                </StackRow>
              </li>
            );
          })}
        </ul>
      </StackSection>

      <StackSection
        id="model"
        label={t("model")}
        value={model ?? t("modelsUnavailable")}
        expanded={open === "model"}
        onToggle={() => toggle("model")}
      >
        <ul aria-label={t("model")} className={STACK_LIST}>
          {modelGroups.map(([provider, providerModels]) => (
            <li key={provider}>
              <div className="px-3 pb-1 pt-2 text-[10px] font-semibold uppercase tracking-wide text-muted-foreground/70 first:pt-1">
                {providerLabel(provider)}
              </div>
              <ul>
                {providerModels.map((candidate) => {
                  const selected = candidate === model;
                  const price = models?.[candidate]?.pricing;
                  return (
                    <li key={candidate}>
                      <StackRow selected={selected} onClick={() => pick("model", () => onModel(candidate))}>
                        <span className={cn(MIN_W_0, FLEX_1)}>
                          <span className="block truncate">{candidate}</span>
                          {prices && price != null && (
                            <span className="block truncate text-[10px] tabular-nums text-muted-foreground">
                              {formatModelPrice(price.input)} / {formatModelPrice(price.cache_read)} /{" "}
                              {formatModelPrice(price.output)}
                            </span>
                          )}
                        </span>
                      </StackRow>
                    </li>
                  );
                })}
              </ul>
            </li>
          ))}
          {modelGroups.length === 0 && (
            <li className="px-3 py-2 text-muted-foreground">{t("modelsUnavailable")}</li>
          )}
        </ul>
      </StackSection>

      <StackSection
        id="effort"
        label={t("reasoningEffort")}
        value={effort ?? "—"}
        expanded={open === "effort"}
        onToggle={() => toggle("effort")}
      >
        <ul aria-label={t("reasoningEffort")} className={STACK_LIST}>
          {efforts.map((level) => (
            <li key={level}>
              <StackRow selected={level === effort} onClick={() => pick("effort", () => onEffort(level))}>
                <span className="truncate">{level}</span>
              </StackRow>
            </li>
          ))}
          {efforts.length === 0 && (
            <li className="px-3 py-2 text-muted-foreground">—</li>
          )}
        </ul>
      </StackSection>
    </div>
  );
}
