"use client";

// meiva:note
// Harness choice for a new agent: the usable rows are plain text, the unusable
// ones are greyed and carry one lowercase word. No dot, no colour bar, no icon and
// no diagnostic sentence - the form asks one question and the row answers it with
// its own state.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md

import * as Popover from "@radix-ui/react-popover";
import { ChevronDown } from "lucide-react";
import { useTranslations } from "next-intl";

import { cn } from "@/lib/format/utils";
import { MIN_W_0, FLEX } from "@/lib/layout/layout";
import {
  harnessLabel,
  harnessRows,
  type Harness,
  type HarnessAvailability,
} from "@/lib/harness/harness";
import { useHarnessAvailability } from "@/lib/harness/use-harness-availability";

export interface HarnessPickerProps {
  machine: string | undefined;
  value: Harness;
  onChange: (harness: Harness) => void;
}

// meiva:note
// One bit answers the only question the form asks. A harness the build cannot start
// is unavailable even when the machine has it installed and signed in; the probe's
// `status` and `reason` stay on the wire for agents and diagnosis.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
function usable(row: HarnessAvailability): boolean {
  return row.launchable;
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
          <span className="truncate">{harnessLabel(selected.harness)}</span>
          <ChevronDown className="size-3 shrink-0 opacity-50" />
        </button>
      </Popover.Trigger>
      <Popover.Portal>
        <Popover.Content
          sideOffset={6}
          align="start"
          className="z-50 w-[220px] max-w-[90vw] rounded-md border border-border bg-popover text-popover-foreground shadow-md outline-none"
        >
          <ul className="py-1">
            {rows.map((row) => (
              <li key={row.harness}>
                <button
                  type="button"
                  disabled={!usable(row)}
                  onClick={() => onChange(row.harness)}
                  className={cn(
                    "w-full text-left px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50",
                  )}
                >
                  <span className={cn(FLEX, "items-center gap-2")}>
                    <span className="truncate">{harnessLabel(row.harness)}</span>
                    {usable(row) ? null : (
                      <span className="ml-auto text-[11px] text-muted-foreground">
                        {t("harnessStatus.unavailable")}
                      </span>
                    )}
                  </span>
                </button>
              </li>
            ))}
          </ul>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
