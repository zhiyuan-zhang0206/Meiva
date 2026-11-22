"use client";

import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ExternalLink } from "lucide-react";
import { useTranslations } from "next-intl";
import Link from "next/link";
import { useCallback, useEffect, useRef } from "react";

import { ScrollArea } from "@/components/ui/scroll-area";
import type { AgentInspectStatistics } from "@/lib/contracts/types";
import { cn } from "@/lib/format/utils";
import { useInspectorHours, useInspectorOpen } from "@/lib/inspector/inspector-panel-store";
import {
  fetchWindowedInspect,
  inspectLiveQueryKey,
  inspectWindowedQueryKey,
} from "@/lib/inspector/inspector-queries";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { INSPECTOR_RETENTION_MS } from "@/lib/layout/switch-budget";
import { useEventStream } from "@/lib/transport/useEventStream";

import {
  ContextSection,
  CostActivitySection,
  useAgentHarness,
} from "./inspector-cost-section";
import {
  ConfigOverlaySection,
  LivenessSection,
  SchedulesSection,
  ShellsSection,
} from "./inspector-live-sections";
import { useApi } from "@/lib/transport/transport-context";

function matchesInspectWindow(
  data: AgentInspectStatistics | undefined,
  agentId: number,
  hours: number | null,
): data is AgentInspectStatistics {
  if (data?.agent_id !== agentId) return false;
  return (data.window_hours ?? null) === hours;
}

export function InspectorPanel({ agentId }: { agentId: number }) {
  const client = useApi();
  const t = useTranslations("inspector");
  const harness = useAgentHarness(agentId);
  const { open } = useInspectorOpen();
  const { inspectorHours: hours, setInspectorHours: setHours } = useInspectorHours();
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
  const liveData =
    liveQuery.data?.agent_id === agentId ? liveQuery.data : undefined;
  const windowedData = matchesInspectWindow(windowedQuery.data, agentId, hours)
    ? windowedQuery.data
    : undefined;

  // meiva:note
  // The Cost + Activity section header owns the panel's only manual refresh and its window picker. The refresh re-fires only the windowed statistics read; the picker re-scopes that same read and offers only cumulative-since-spawn (All), the last 5m, and the supported 1h / 24h / 7d windows. Neither control touches the current-state read, and a retention-clamped selection labels itself with the applied hours.
  // meiva:link ui/web/src/docs/frontend-components/inspector/queries-and-refresh.md
  const refreshCostActivity = useCallback(() => {
    void windowedQuery.refetch();
  }, [windowedQuery]);

  useEffect(() => {
    if (!open) {
      void queryClient.cancelQueries({ queryKey: inspectLiveQueryKey(agentId) });
      void queryClient.cancelQueries({ queryKey: ["agent-inspect", agentId] });
    }
  }, [agentId, open, queryClient]);

  const scrollRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const viewport = scrollRef.current?.querySelector<HTMLDivElement>(
      '[data-slot="scroll-area-viewport"]',
    );
    if (viewport) viewport.scrollTop = 0;
  }, [agentId]);

  const onConnectionEvent = useCallback(
    (event: { type: string }) => {
      if (open && event.type === "open") {
        void queryClient.invalidateQueries({ queryKey: inspectLiveQueryKey(agentId) });
      }
    },
    [agentId, open, queryClient],
  );
  // meiva:link ui/web/src/docs/frontend-components/inspector/queries-and-refresh.md
  useEventStream(() => undefined, onConnectionEvent);

  if (!open) return null;

  return (
    <aside className={cn("h-full w-full bg-background", FLEX, FLEX_COL, MIN_H_0)}>
      <ScrollArea ref={scrollRef} className={cn("text-[11.5px]", MIN_H_0, FLEX_1)}>
        <div className="px-4 py-3">
          <div className="space-y-3.5">
            {harness === "meiva" && (
              <div className={cn("items-center gap-2", FLEX)}>
                <Link
                  href={`/insights/run/${agentId}`}
                  className={cn(
                    "items-center gap-2 rounded border border-black/[0.06] dark:border-white/[0.08] bg-sidebar-accent/40 dark:bg-[#33322C] px-2.5 py-1.5 font-mono text-[11.5px] leading-[17px] text-foreground dark:text-[#f0ede7] hover:bg-sidebar-accent group",
                    FLEX,
                    MIN_W_0,
                    FLEX_1,
                  )}
                >
                  <ExternalLink className="size-3 shrink-0 text-muted-foreground group-hover:text-foreground" aria-hidden />
                  <span className="truncate">{t("openRunTimeline")}</span>
                </Link>
              </div>
            )}
            {liveData && (
              <>
                <ShellsSection inspect={liveData} />
                <SchedulesSection inspect={liveData} />
              </>
            )}
            <LivenessSection
              agentId={agentId}
              inspect={liveData}
              pending={liveQuery.isPending}
              errorMessage={liveQuery.error instanceof Error ? liveQuery.error.message : t("noData")}
              fetching={liveQuery.isFetching}
              onRetry={() => void liveQuery.refetch()}
            />
            {liveData && <ConfigOverlaySection inspect={liveData} />}
            <ContextSection agentId={agentId} />
            <CostActivitySection
              agentId={agentId}
              inspect={windowedData}
              failed={windowedQuery.error != null}
              pending={windowedQuery.isPending}
              hideSpinner={!liveData && liveQuery.isPending}
              hours={hours}
              onHoursChange={setHours}
              fetching={windowedQuery.isFetching}
              onRefresh={refreshCostActivity}
            />
          </div>
        </div>
      </ScrollArea>
    </aside>
  );
}
