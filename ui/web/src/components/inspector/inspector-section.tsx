import type { ReactNode } from "react";

import { FLEX, FLEX_COL } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export function Section({
  icon,
  title,
  badge,
  action,
  loading,
  children,
}: {
  icon: ReactNode;
  title: string;
  badge?: string;
  action?: ReactNode;
  loading?: boolean;
  children: ReactNode;
}) {
  return (
    <section
      className="space-y-1.5"
      aria-label={loading ? `${title} loading` : undefined}
      aria-busy={loading ? true : undefined}
    >
      <div className={cn("items-center gap-1.5 text-[11px] font-medium tracking-wide text-foreground/75 dark:text-[#ccc9c2]", FLEX)}>
        {icon}
        <span>{title}</span>
        {badge != null && (
          <span className="ml-auto font-mono text-[11px] tabular-nums text-foreground dark:text-[#f0ede7] normal-case">
            {badge}
          </span>
        )}
        {action != null && (
          <span className={cn("shrink-0 items-center", FLEX, badge == null && "ml-auto")}>
            {action}
          </span>
        )}
      </div>
      {children}
    </section>
  );
}

export function Metric({
  className,
  label,
  value,
  sub,
  valueTestId,
}: {
  className?: string;
  label: string;
  value: string | undefined;
  sub?: string;
  valueTestId?: string;
}) {
  return (
    <div className={cn("gap-0.5 rounded border border-black/[0.06] dark:border-white/[0.08] bg-sidebar-accent/40 dark:bg-[#33322C] px-2.5 py-1.5", FLEX, FLEX_COL, className)}>
      <span className="text-[10.5px] tracking-wide text-muted-foreground">{label}</span>
      {value === undefined ? (
        <span className={cn("h-[17px] items-center", FLEX)} aria-hidden="true">
          <span className="h-3 w-12 animate-pulse rounded-sm bg-muted-foreground/15" />
        </span>
      ) : (
        <span className="font-mono text-[11.5px] leading-[17px] tabular-nums text-foreground dark:text-[#f0ede7]" data-testid={valueTestId}>
          {value}
        </span>
      )}
      {sub != null && (
        <span className="font-mono text-[10px] tabular-nums text-muted-foreground/85">{sub}</span>
      )}
    </div>
  );
}
