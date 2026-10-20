"use client";

import { useTranslations } from "next-intl";

import { useBreakpoint } from "@/lib/layout/breakpoint";
import { ScrollArea } from "@/components/ui/scroll-area";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export function SectionSkeleton({
  title,
  rows = 2,
}: {
  title: string;
  rows?: number;
}) {
  return (
    <section aria-label={`${title} loading`} className="space-y-1.5">
      <div className="h-3 w-24 animate-pulse rounded bg-muted-foreground/20" />
      <div className="grid grid-cols-2 gap-1">
        {Array.from({ length: rows }, (_, index) => (
          <div
            key={index}
            className="h-10 animate-pulse rounded bg-muted-foreground/10"
          />
        ))}
      </div>
    </section>
  );
}

export function LiveSectionsSkeleton() {
  const t = useTranslations("inspector");
  return (
    <>
      <SectionSkeleton title={t("sectionShells")} rows={1} />
      <SectionSkeleton title={t("sectionLiveness")} rows={3} />
      <SectionSkeleton title={t("sectionConfigOverlay")} rows={1} />
      <SectionSkeleton title={t("sectionContext")} rows={1} />
    </>
  );
}

export function WindowedSectionsSkeleton() {
  const t = useTranslations("inspector");
  return <SectionSkeleton title={t("sectionCostActivity")} rows={4} />;
}

// meiva:note
// The placeholder stands in for the panel an inspector actually renders, so it
// mirrors INSPECT_SECTION_ORDER (run link, page, shells, liveness, config
// overlay, context, cost/activity) and draws no header: the embedded panel has
// none, and a skeleton carrying the word "Inspector" over a different set of
// blocks made the panel jump when the real content arrived (user report
// 2026-10-20).
// meiva:link ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md
export function InspectorPanelSkeleton() {
  const t = useTranslations("inspector");
  const { isLarge } = useBreakpoint();

  const body = (
    <ScrollArea className={cn("text-xs", MIN_H_0, FLEX_1)}>
      <div className="px-4 py-3">
        <div className="space-y-4">
          <SectionSkeleton title={t("sectionPage")} rows={2} />
          <LiveSectionsSkeleton />
          <WindowedSectionsSkeleton />
        </div>
      </div>
    </ScrollArea>
  );

  if (isLarge) {
    return (
      <aside
        data-testid="inspector-panel-skeleton"
        className={cn("h-full w-full bg-background", FLEX, FLEX_COL, MIN_H_0)}
      >
        {body}
      </aside>
    );
  }

  return (
    <div className={cn("fixed inset-0 z-50", FLEX)}>
      <div className="absolute inset-0 bg-black/40" aria-hidden="true" />
      <aside
        data-testid="inspector-panel-skeleton"
        className={cn("relative w-full bg-background", FLEX, FLEX_COL)}
      >
        {body}
      </aside>
    </div>
  );
}
