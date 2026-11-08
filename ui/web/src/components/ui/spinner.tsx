"use client";

import { Loader2 } from "lucide-react";

import { cn } from "@/lib/format/utils";

// meiva:note
// The one loading indicator: a turning ring, never text and never a placeholder
// shaped like the content it stands in for (user ruling 2026-11-08 — a loading
// state shows the spinner alone). `label` is the accessible name for a spinner
// that replaces a read whose subject a screen reader cannot see; it is an
// attribute, not visible text, so the spinner never paints a word. A reader who
// asked for reduced motion gets the same ring breathing instead of a frozen one.
export function Spinner({
  className,
  label,
}: {
  className?: string;
  label?: string;
}) {
  return (
    <Loader2
      role={label === undefined ? undefined : "status"}
      aria-label={label}
      aria-hidden={label === undefined ? true : undefined}
      className={cn(
        "size-3.5 shrink-0 animate-spin text-muted-foreground motion-reduce:animate-pulse",
        className,
      )}
    />
  );
}
