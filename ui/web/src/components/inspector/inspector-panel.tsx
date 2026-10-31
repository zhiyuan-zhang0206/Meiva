"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink, X } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { Fragment, useCallback, useEffect, useRef, type ReactNode } from "react";

import { LiveSectionsSkeleton, SectionSkeleton } from "@/components/inspector/inspector-panel-skeleton";
import { InspectWidgetSection } from "@/components/inspector/inspector-widgets";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useAgentPages } from "@/lib/agents/use-agent-pages";
import type {
  AgentInspectStatistics,
  SystemEvent,
} from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { useInspectorHours, useInspectorOpen } from "@/lib/inspector/inspector-panel-store";
import {
  fetchWindowedInspect,
  inspectLiveQueryKey,
  inspectWidgetsQueryKey,
  inspectWindowedQueryKey,
} from "@/lib/inspector/inspector-queries";
import { INSPECT_SECTION_ORDER } from "@/lib/inspector/inspector-widgets";
import { useBreakpoint } from "@/lib/layout/breakpoint";
import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS, FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { INSPECTOR_RETENTION_MS } from "@/lib/layout/switch-budget";
import { useEventStream } from "@/lib/transport/useEventStream";

import {
  ContextSection,
  CostActivitySection,
  InspectReadError,
} from "./inspector-cost-section";
import {
  ConfigOverlaySection,
  LivenessSection,
  NoticeReplySection,
  PageSection,
  ShellsSection,
} from "./inspector-live-sections";
import { useApi } from "@/lib/transport/transport-context";

function StaleDot() {
  const t = useTranslations("inspector");
  return (
    <span
      aria-label={t("liveRefreshFailing")}
      className="size-1.5 shrink-0 rounded-full bg-amber-500"
    />
  );
}

function matchesInspectWindow(
  data: AgentInspectStatistics | undefined,
  agentId: number,
  hours: number | null,
): data is AgentInspectStatistics {
  if (data?.agent_id !== agentId) return false;
  return (data.window_hours ?? null) === hours;
}

export function InspectorPanel({ agentId, embedded = false }: { agentId: number; embedded?: boolean }) {
  const client = useApi();
  const t = useTranslations("inspector");
  const { open, toggle } = useInspectorOpen();
  const { inspectorHours: hours, setInspectorHours: setHours } = useInspectorHours();
  const { isLarge } = useBreakpoint();
  const queryClient = useQueryClient();

  const liveQuery = useQuery({
    queryKey: inspectLiveQueryKey(agentId),
    queryFn: ({ signal }) => client.getAgentInspectLive(agentId, signal),
    enabled: open,
    retry: false,
    staleTime: 0,
    gcTime: INSPECTOR_RETENTION_MS,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: open ? 60_000 : false,
    refetchOnMount: "always",
  });
  const windowedQuery = useQuery({
    queryKey: inspectWindowedQueryKey(agentId, hours),
    queryFn: ({ signal }) => fetchWindowedInspect(client, agentId, hours, signal),
    enabled: open,
    retry: false,
    staleTime: 0,
    gcTime: INSPECTOR_RETENTION_MS,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: open ? 60_000 : false,
    refetchOnMount: "always",
  });
  const widgetsQuery = useQuery({
    queryKey: inspectWidgetsQueryKey(agentId),
    queryFn: ({ signal }) => client.getAgentInspectWidgets(agentId, signal),
    enabled: open,
    retry: false,
    staleTime: INSPECTOR_RETENTION_MS,
    gcTime: INSPECTOR_RETENTION_MS,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: open ? 60_000 : false,
  });

  const liveData =
    liveQuery.data?.agent_id === agentId ? liveQuery.data : undefined;
  const windowedData = matchesInspectWindow(windowedQuery.data, agentId, hours)
    ? windowedQuery.data
    : undefined;
  const hasStaleError =
    (liveQuery.error !== null && liveData !== undefined) ||
    (windowedQuery.error !== null && windowedData !== undefined) ||
    (widgetsQuery.error !== null && widgetsQuery.data !== undefined);

  // meiva:note
  // The Cost + Activity section header owns the panel's only manual refresh and its window picker. The refresh re-fires only the windowed statistics read; the picker re-scopes that same read and offers only cumulative-since-spawn (All), the last 5m, and the supported 1h / 24h / 7d windows. Neither control touches the current-state or widget reads, and a retention-clamped selection labels itself with the applied hours.
  // meiva:link ui/web/src/docs/frontend-components/inspector/queries-and-refresh.meiva.okf.md
  const refreshCostActivity = useCallback(() => {
    void windowedQuery.refetch();
  }, [windowedQuery]);

  useEffect(() => {
    if (!open) {
      void queryClient.cancelQueries({ queryKey: inspectLiveQueryKey(agentId) });
      void queryClient.cancelQueries({ queryKey: ["agent-inspect", agentId] });
      void queryClient.cancelQueries({ queryKey: inspectWidgetsQueryKey(agentId) });
    }
  }, [agentId, open, queryClient]);

  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const viewport = scrollRef.current?.querySelector<HTMLDivElement>(
      '[data-slot="scroll-area-viewport"]',
    );
    if (viewport) viewport.scrollTop = 0;
  }, [agentId]);

  const pages = useAgentPages(agentId);

  const onSystemEvent = useCallback(
    (ev: SystemEvent) => {
      if (!open || ev.agent_id !== agentId) return;
      if (ev.role === "task_created" || ev.role === "task_updated") {
        void queryClient.invalidateQueries({ queryKey: inspectWidgetsQueryKey(agentId) });
      }
    },
    [agentId, open, queryClient],
  );
  const onConnectionEvent = useCallback(
    (event: { type: string }) => {
      if (open && event.type === "open") {
        void queryClient.invalidateQueries({ queryKey: inspectLiveQueryKey(agentId) });
        void queryClient.invalidateQueries({ queryKey: inspectWidgetsQueryKey(agentId) });
      }
    },
    [agentId, open, queryClient],
  );
  useEventStream(onSystemEvent, onConnectionEvent);

  if (!open) return null;

  // meiva:link ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md
  const sections: { order: number; tie: number; key: string; node: ReactNode }[] = [];
  const runLinkNode = (
    <div className={cn("items-center gap-2", FLEX)}>
      <Link
        href={`/insights/run/${agentId}`}
        className={cn(
          "items-center gap-2 rounded bg-sidebar-accent/40 dark:bg-[#2a2926] px-2.5 py-1.5 font-mono text-[11.5px] leading-[17px] text-foreground dark:text-[#f0ede7] hover:bg-sidebar-accent group",
          FLEX,
          MIN_W_0,
          FLEX_1,
        )}
      >
        <ExternalLink className="size-3 shrink-0 text-muted-foreground group-hover:text-foreground" aria-hidden />
        <span className="truncate">{t("openRunTimeline")}</span>
      </Link>
      {hasStaleError ? <span className="shrink-0"><StaleDot /></span> : null}
    </div>
  );
  sections.push({
    order: INSPECT_SECTION_ORDER.runLink,
    tie: 0,
    key: "run-link",
    node: runLinkNode,
  });
  sections.push({
    order: INSPECT_SECTION_ORDER.costActivity,
    tie: 0,
    key: "cost-activity",
    node: (
      <CostActivitySection
        agentId={agentId}
        inspect={windowedData}
        failed={windowedQuery.error != null}
        pending={windowedQuery.isPending}
        hours={hours}
        onHoursChange={setHours}
        fetching={windowedQuery.isFetching}
        onRefresh={refreshCostActivity}
      />
    ),
  });
  sections.push({
    order: INSPECT_SECTION_ORDER.context,
    tie: 0,
    key: "context",
    node: <ContextSection agentId={agentId} />,
  });
  sections.push({ order: INSPECT_SECTION_ORDER.page, tie: 0, key: "page", node: <PageSection pages={pages} /> });
  if (liveData) {
    sections.push(
      { order: INSPECT_SECTION_ORDER.shells, tie: 0, key: "shells", node: <ShellsSection inspect={liveData} /> },
      { order: INSPECT_SECTION_ORDER.liveness, tie: 0, key: "liveness", node: <LivenessSection inspect={liveData} /> },
      { order: INSPECT_SECTION_ORDER.configOverlay, tie: 0, key: "config-overlay", node: <ConfigOverlaySection inspect={liveData} /> },
    );
  } else {
    sections.push({
      order: INSPECT_SECTION_ORDER.shells,
      tie: 0,
      key: "live-state",
      node: !liveQuery.isPending ? (
        <InspectReadError
          message={liveQuery.error instanceof Error ? liveQuery.error.message : t("noData")}
          retryLabel={t("retryInspector")}
          pending={liveQuery.isFetching}
          onRetry={() => void liveQuery.refetch()}
        />
      ) : <LiveSectionsSkeleton />,
    });
  }
  if (liveData?.notice && !embedded) {
    sections.push({
      order: INSPECT_SECTION_ORDER.notice,
      tie: 0,
      key: "notice",
      node: <NoticeReplySection agentId={agentId} notice={liveData.notice} />,
    });
  }
  if (widgetsQuery.isPending || (widgetsQuery.error && !widgetsQuery.data)) {
    sections.push({
      order: INSPECT_SECTION_ORDER.page,
      tie: 1,
      key: "widgets-state",
      node: widgetsQuery.error ? (
        <InspectReadError
          message={t("widgetsUnavailable")}
          retryLabel={t("retryWidgets")}
          pending={widgetsQuery.isFetching}
          onRetry={() => void widgetsQuery.refetch()}
        />
      ) : <SectionSkeleton title={t("sectionWidgets")} rows={1} />,
    });
  }
  for (const widget of widgetsQuery.data ?? []) {
    sections.push({
      order: widget.order,
      tie: 1,
      key: `widget:${widget.plugin}/${widget.id}`,
      node: <InspectWidgetSection widget={widget} />,
    });
  }
  sections.sort(
    (a, b) => a.order - b.order || a.tie - b.tie || (a.key < b.key ? -1 : a.key > b.key ? 1 : 0),
  );

  const body = (
    <>
      {embedded ? null : (
        <header className={cn("relative items-center gap-2 px-4", BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS, FLEX)}>
          {!isLarge ? (
            <button
              type="button"
              onClick={toggle}
              aria-label={t("closeInspector")}
              className="shrink-0 rounded p-1 -ml-1 text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
            >
              <X className="size-5" />
            </button>
          ) : null}
          <span className={cn("truncate font-mono text-[11px] tracking-wide text-muted-foreground", MIN_W_0, FLEX_1)}>
            {t("title")}
          </span>
        </header>
      )}

      <ScrollArea ref={scrollRef} className={cn("text-[11.5px]", MIN_H_0, FLEX_1)}>
        <div className="px-4 py-3">
          <div className="space-y-3.5">
            {sections.map((section) => (
              <Fragment key={section.key}>{section.node}</Fragment>
            ))}
          </div>
        </div>
      </ScrollArea>
    </>
  );

  if (isLarge || embedded) {
    return (
      <aside className={cn("h-full w-full bg-background", FLEX, FLEX_COL, MIN_H_0)}>
        {body}
      </aside>
    );
  }

  return (
    <div className={cn("fixed inset-0 z-50", FLEX)}>
      <div
        className="absolute inset-0 bg-black/40"
        onClick={toggle}
        aria-hidden="true"
      />
      <aside className={cn("relative w-full bg-background", FLEX, FLEX_COL)}>{body}</aside>
    </div>
  );
}
