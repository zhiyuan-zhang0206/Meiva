"use client";

import { useTranslations } from "next-intl";

import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";

// meiva:note
// The run timeline's loading state: the panel's own frame, a spinner, nothing
// else (user ruling 2026-11-08 — no skeleton, no loading sentence). The frame
// keeps the height the resolved chart occupies so the page does not jump when
// the read lands.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/run-timeline.meiva.okf.md
export function RunTimelineChartLoading() {
  const t = useTranslations("runTimeline");
  return (
    <section
      className={cn(
        "min-h-[131px] items-center justify-center rounded-[10px] border border-border bg-card p-3",
        FLEX,
      )}
    >
      <Spinner className="size-5" label={t("loading")} />
    </section>
  );
}
