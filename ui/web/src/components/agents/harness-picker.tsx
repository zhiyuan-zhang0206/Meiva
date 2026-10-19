"use client";

// meiva:note
// Harness choice for a new agent, with what the target machine can actually run. Rows come from the availability probe; a harness that is not `available` cannot be picked, and one that is installed but not `launchable` can be picked only to be refused at spawn, with the reason on screen - a harness the platform cannot start must not look like a working choice. When the gateway has no probe at all, the built-in `meiva` runtime stays selectable and every external harness reads `unknown`, never `unavailable`.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md

import * as Popover from "@radix-ui/react-popover";
import { ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/format/utils";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import {
  harnessLabel,
  harnessRows,
  type Harness,
  type HarnessAvailability,
} from "@/lib/harness/harness";
import { useHarnessAvailability } from "@/lib/harness/use-harness-availability";

const STATUS_DOT: Record<HarnessAvailability["status"], string> = {
  available: "bg-emerald-500",
  unavailable: "bg-muted-foreground/40",
  unknown: "bg-amber-500",
};

export interface HarnessPickerProps {
  machine: string | undefined;
  value: Harness;
  onChange: (harness: Harness) => void;
}

export function HarnessPicker({ machine, value, onChange }: HarnessPickerProps) {
  const t = useTranslations("spawn");
  const availability = useHarnessAvailability(machine);
  const rows = harnessRows(availability.data ?? []);
  const selected = rows.find((row) => row.harness === value) ?? rows[0];

  return (
    <Popover.Root>
      <Popover.Trigger asChild>
        <button
          type="button"
          aria-label={t("harness")}
          className={cn(
            "inline-flex items-center gap-1 truncate text-xs bg-transparent border border-border rounded px-1 py-0.5",
            "text-muted-foreground hover:text-foreground focus:outline-none focus:ring-1 focus:ring-ring cursor-pointer",
            MIN_W_0,
          )}
        >
          <span className={cn("size-1.5 rounded-full shrink-0", STATUS_DOT[selected.status])} aria-hidden="true" />
          <span className="truncate">{harnessLabel(selected.harness)}</span>
          <ChevronDown className="size-3 shrink-0 opacity-50" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          sideOffset={6}
          align="start"
          className="z-50 w-[300px] max-w-[90vw] rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none"
        >
          <ul className="py-1">
            {rows.map((row) => {
              const selectable = row.status !== "unavailable";
              return (
                <li key={row.harness}>
                  <button
                    type="button"
                    disabled={!selectable}
                    title={row.reason || undefined}
                    onClick={() => onChange(row.harness)}
                    className={cn(
                      "w-full text-left px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50",
                    )}
                  >
                    <span className={cn(FLEX, "items-center gap-2")}>
                      <span className={cn("size-1.5 rounded-full shrink-0", STATUS_DOT[row.status])} aria-hidden="true" />
                      <span className="truncate">{harnessLabel(row.harness)}</span>
                      <span className="ml-auto text-[10px] uppercase tracking-wide text-muted-foreground">
                        {t(`harnessStatus.${row.status}`)}
                      </span>
                    </span>
                    {row.reason ? (
                      <span className="block mt-0.5 text-[11px] text-muted-foreground/80">{row.reason}</span>
                    ) : null}
                  </button>
                </li>
              );
            })}
          </ul>
          {!selected.launchable ? (
            <div role="status" className="px-3 py-2 border-t border-border text-[11px] text-amber-600 dark:text-amber-400">
              {t("harnessNotLaunchable", { harness: harnessLabel(selected.harness) })}
            </div>
          ) : null}
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
