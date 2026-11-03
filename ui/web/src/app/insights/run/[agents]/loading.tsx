"use client";

import { useTranslations } from "next-intl";

import { RunTimelineChartSkeleton } from "@/components/run-timeline/run-timeline-skeleton";
import { RunTimelineWorkspace } from "@/components/run-timeline/run-timeline-workspace";
import { PageHeader } from "@/components/shell/page-header";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

export default function Loading() {
  const t = useTranslations("runTimeline");
  return (
    <main id="main-content" className={cn(FLEX, FLEX_1, MIN_H_0, FLEX_COL)}>
      <PageHeader title={t("title")} backHref="/insights" backLabel={t("backToInsights")} />
      <RunTimelineWorkspace
        main={<RunTimelineChartSkeleton />}
        side={<p className="text-sm text-muted-foreground">{t("detailEmpty")}</p>}
      />
    </main>
  );
}
