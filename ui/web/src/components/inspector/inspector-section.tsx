import type { ReactNode } from "react";

import { FLEX, FLEX_COL } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export function Section({
  icon,
  title,
  badge,
  action,
  children,
}: {
  icon: ReactNode;
  title: string;
  badge?: string;
  action?: ReactNode;
  children: ReactNode;
}) {
  return (
    <section className="space-y-1.5">
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
  value: string;
  sub?: string;
  valueTestId?: string;
}) {
  return (
    <div className={cn("gap-0.5 rounded bg-sidebar-accent/40 dark:bg-[#2a2926] px-2.5 py-1.5", FLEX, FLEX_COL, className)}>
      <span className="text-[10.5px] tracking-wide text-muted-foreground">{label}</span>
      <span className="font-mono text-[11.5px] leading-[17px] tabular-nums text-foreground dark:text-[#f0ede7]" data-testid={valueTestId}>
        {value}
      </span>
      {sub != null && (
        <span className="font-mono text-[10px] tabular-nums text-muted-foreground/85">{sub}</span>
      )}
    </div>
  );
}
