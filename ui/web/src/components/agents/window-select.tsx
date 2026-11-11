import { ChevronDown } from "lucide-react";

import { cn } from "@/lib/format/utils";

// WindowSelect — the project's one time-window dropdown.
//
// Extracted from the standard pattern that already existed in several places
// (the sidebar's stats window, the fleet Graph View's window, and the Task
// Graph's time filter) — user ruling 2026-08-30: range pickers must reuse one
// shared component instead of spawning a new variant per surface. Each
// surface passes its own option set and className; the control is identical.

export interface WindowOption {
  readonly value: string;
  readonly label: string;
}

export function WindowSelect({
  value,
  options,
  onChange,
  ariaLabel,
  className,
}: {
  value: string;
  options: readonly WindowOption[];
  onChange: (value: string) => void;
  ariaLabel: string;
  className?: string;
}) {
  // meiva:note
  // Keep the native picker at 16px to prevent iOS focus zoom. Its visible
  // label inherits the compact surface typography independently.
  // meiva:link ui/web/src/docs/frontend-components/inspector/queries-and-refresh.md
  return (
    <span className={cn("relative inline-flex items-center gap-1 focus-within:ring-1 focus-within:ring-ring", className)}>
      <span aria-hidden="true">{options.find((option) => option.value === value)?.label}</span>
      <ChevronDown aria-hidden="true" className="size-3 shrink-0" />
      <select
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label={ariaLabel}
        className="absolute inset-0 h-full w-full cursor-pointer text-base opacity-0"
      >
        {options.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </span>
  );
}
