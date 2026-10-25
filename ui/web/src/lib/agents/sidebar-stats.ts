import { keepPreviousData, type QueryClient, type UseQueryResult, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect } from "react";

import type { StatsDashboard } from "../contracts/types";
import { useUserSettings } from "../state/use-user-settings";
import { api, type Api } from "../transport/api";
import { useApi } from "../transport/transport-context";

export const STATS_WINDOWS = [0, 1, 6, 24, 72, 168] as const;
export type StatsWindowHours = (typeof STATS_WINDOWS)[number];
export const STATS_WINDOW_LABELS: Record<StatsWindowHours, string> = {
  0: "5m",
  1: "1h",
  6: "6h",
  24: "24h",
  72: "3d",
  168: "7d",
};
export const STATS_WINDOW_DEFAULT: StatsWindowHours = 24;

export function useStatsWindow(): {
  windowHours: StatsWindowHours;
  setWindowHours: (h: StatsWindowHours) => void;
} {
  const { settings, setSetting } = useUserSettings();
  const raw = settings["display.stats_window_hours"];
  const windowHours: StatsWindowHours =
    typeof raw === "number" && (STATS_WINDOWS as readonly number[]).includes(raw)
      ? (raw as StatsWindowHours)
      : STATS_WINDOW_DEFAULT;
  const setWindowHours = useCallback(
    (h: StatsWindowHours) => setSetting("display.stats_window_hours", h),
    [setSetting],
  );
  return { windowHours, setWindowHours };
}

const STATS_POLL_MS = 30_000;
const STATS_POLL_MAX_MS = 240_000;

interface StatsPollEntry {
  subscribers: number;
  timer: ReturnType<typeof setTimeout> | undefined;
  delayMs: number;
  failureCount: number;
}

const statsPollers = new WeakMap<QueryClient, Map<StatsWindowHours, StatsPollEntry>>();

function subscribeStatsPoll(
  queryClient: QueryClient,
  windowHours: StatsWindowHours,
  client: Api,
): () => void {
  let byWindow = statsPollers.get(queryClient);
  if (byWindow === undefined) {
    byWindow = new Map();
    statsPollers.set(queryClient, byWindow);
  }
  let entry = byWindow.get(windowHours);
  if (entry === undefined) {
    const created: StatsPollEntry = {
      subscribers: 0,
      timer: undefined,
      delayMs: STATS_POLL_MS,
      failureCount: 0,
    };
    const schedule = () => {
      if (byWindow.get(windowHours) !== created) return;
      created.timer = setTimeout(run, created.delayMs);
    };
    const run = () => {
      created.timer = undefined;
      void queryClient
        .query({
          queryKey: ["stats", "dashboard", windowHours],
          queryFn: () => client.getStatsDashboard(windowHours),
          retry: 1,
          staleTime: 0,
        })
        .then(
          () => {
            created.failureCount = 0;
            created.delayMs = STATS_POLL_MS;
          },
          () => {
            created.failureCount += 1;
            created.delayMs = Math.min(created.delayMs * 2, STATS_POLL_MAX_MS);
          },
        )
        .catch(() => undefined)
        .finally(schedule);
    };
    created.timer = setTimeout(run, created.delayMs);
    entry = created;
    byWindow.set(windowHours, entry);
  }
  entry.subscribers += 1;
  return () => {
    const current = byWindow.get(windowHours);
    if (current === undefined) return;
    current.subscribers -= 1;
    if (current.subscribers === 0) {
      if (current.timer !== undefined) clearTimeout(current.timer);
      byWindow.delete(windowHours);
    }
  };
}

export function useStatsDashboard(windowHours: StatsWindowHours): {
  stats: StatsDashboard | undefined;
  error: unknown;
  isFetching: boolean;
  refetch: UseQueryResult<StatsDashboard>["refetch"];
} {
  const client = useApi(api);
  const queryClient = useQueryClient();
  const { data: stats, error, isFetching, refetch } = useQuery({
    queryKey: ["stats", "dashboard", windowHours],
    queryFn: ({ signal }) => client.getStatsDashboard(windowHours, signal),
    retry: 1,
    staleTime: STATS_POLL_MS,
    placeholderData: keepPreviousData,
  });
  useEffect(
    () => subscribeStatsPoll(queryClient, windowHours, client),
    [queryClient, windowHours, client],
  );
  return { stats, error, isFetching, refetch };
}
