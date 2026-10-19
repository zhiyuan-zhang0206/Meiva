"use client";

// meiva:note
// Harness choice for a new agent, in one word per row. The picker answers a single
// question - can this machine start an agent on it here - so a harness that is
// installed but has no launcher reads `unavailable` like one that is not
// installed, and the probe's diagnostic sentence stays on the wire instead of in
// the UI. When the gateway cannot report at all, the built-in runtime stays
// usable and every external harness reads `unavailable`.
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

type ShownStatus = "available" | "unavailable";

const STATUS_DOT: Record<ShownStatus, string> = {
  available: "bg-emerald-500",
  unavailable: "bg-muted-foreground/40",
};

// meiva:note
// One word answers the only question the form asks. A harness the build has no
// launcher for is `unavailable` even when the machine has it installed and signed
// in; the probe's `status` and `reason` stay on the wire for diagnosis and never
// reach the row.
// meiva:link ui/web/src/docs/frontend-components/creation-availability.meiva.okf.md
function shownStatus(row: HarnessAvailability): ShownStatus {
  return row.launchable ? "available" : "unavailable";
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
          <span className={cn("size-1.5 rounded-full shrink-0", STATUS_DOT[shownStatus(selected)])} aria-hidden="true" />
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
            {rows.map((row) => {
              const status = shownStatus(row);
              return (
                <li key={row.harness}>
                  <button
                    type="button"
                    disabled={status === "unavailable"}
                    onClick={() => onChange(row.harness)}
                    className={cn(
                      "w-full text-left px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50",
                    )}
                  >
                    <span className={cn(FLEX, "items-center gap-2")}>
                      <span className={cn("size-1.5 rounded-full shrink-0", STATUS_DOT[status])} aria-hidden="true" />
                      <span className="truncate">{harnessLabel(row.harness)}</span>
                      <span className="ml-auto text-[10px] uppercase tracking-wide text-muted-foreground">
                        {t(`harnessStatus.${status}`)}
                      </span>
                    </span>
                  </button>
                </li>
              );
            })}
          </ul>
        </Popover.Content>
      </Popover.Portal>
    </Popover.Root>
  );
}
