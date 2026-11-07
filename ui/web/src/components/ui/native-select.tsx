import { ChevronDown } from "lucide-react";
import type { ComponentProps } from "react";

import { MIN_W_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export function NativeSelect({ className, ...props }: ComponentProps<"select">) {
  return (
    <span className={cn("relative inline-flex max-w-full", MIN_W_0)}>
      <select
        {...props}
        className={cn(
          "h-9 w-full appearance-none rounded-lg border border-border bg-background py-1.5 pl-3 pr-9 text-sm text-foreground shadow-xs outline-none focus-visible:border-ring focus-visible:ring-2 focus-visible:ring-ring/50 disabled:cursor-not-allowed disabled:opacity-50",
          MIN_W_0,
          className,
        )}
      />
      <ChevronDown aria-hidden className="pointer-events-none absolute right-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" />
    </span>
  );
}
