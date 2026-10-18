"use client";

import { useQueries, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useState } from "react";

import type { RunTimelineResponse } from "@/lib/contracts/types";
import { api, ApiError } from "@/lib/transport/api";

import { viewportOf, type Viewport } from "../model/timeline-model";

const MAX_UNITS = 5000;
const pickRead = (results: { data: RunTimelineResponse | undefined; isError: boolean; isFetching: boolean; error: Error | null; failureReason: Error | null }[]) =>
  results.map(({ data, isError, isFetching, error, failureReason }) => ({ data, isError, isFetching, error, failureReason }));

function preparing(error: Error | null): boolean {
  return error instanceof ApiError &&
    ["timeline_index_queued", "timeline_index_indexing", "timeline_index_busy"].includes(error.code ?? "");
}

function covers(data: RunTimelineResponse | undefined, view: Viewport | null): boolean {
  if (data === undefined || view === null || data.units_omitted > 0) return false;
  const window = viewportOf(data.window);
  return window.from <= view.from && window.to >= view.to;
}

function windowQuery(id: number, snapshot: RunTimelineResponse["snapshot"], range: Viewport | null) {
  return {
    queryKey: ["run-timeline-window", id, snapshot, range?.from, range?.to],
    queryFn: ({ signal }: { signal: AbortSignal }) => {
      if (range === null) throw new Error("a timeline window must have bounds");
      return api.getRunTimeline(id, {
        from: new Date(range.from).toISOString(),
        to: new Date(range.to).toISOString(),
        maxUnits: MAX_UNITS,
        snapshot: snapshot ?? undefined,
        signal,
      });
    },
    staleTime: Infinity,
    gcTime: 60_000,
    retry: false,
  };
}

interface ShownWindow {
  id: number;
  snapshot: RunTimelineResponse["snapshot"];
  range: Viewport | null;
}

function sameWindow(a: ShownWindow, b: ShownWindow | undefined) {
  return a.id === b?.id && a.snapshot === b.snapshot &&
    a.range?.from === b.range?.from && a.range?.to === b.range?.to;
}

// meiva:link ui/web/src/docs/frontend-components/run-timeline/window-reads.meiva.okf.md
export function useTimelineWindows(ids: readonly number[], viewport: Viewport | null) {
  const client = useQueryClient();
  const [settled, setSettled] = useState<Viewport | null>(null);
  useEffect(() => {
    const timer = setTimeout(() => setSettled(viewport), 300);
    return () => clearTimeout(timer);
  }, [viewport]);
  const initialQueries = ids.map((id) => ({
    queryKey: ["run-timeline", id],
    queryFn: ({ signal }: { signal: AbortSignal }) => api.getRunTimeline(id, { recentHours: 24, maxUnits: MAX_UNITS, signal }),
    staleTime: Infinity,
    gcTime: 60_000,
    retry: (attempt: number, error: Error) => preparing(error) && attempt < 30,
    retryDelay: 2_000,
  }));
  const initial = useQueries({ queries: initialQueries, combine: pickRead });
  const windows = useQueries({
    queries: ids.map((id, i) => {
      const data = initial.at(i)?.data;
      return {
        ...windowQuery(id, data?.snapshot, settled),
        enabled: data !== undefined && settled !== null && !covers(data, settled),
      };
    }),
    combine: pickRead,
  });

  const [shown, setShown] = useState<ShownWindow[]>([]);
  const ready = viewport?.from === settled?.from && viewport?.to === settled?.to;
  const nextShown = ids.map((id, i): ShownWindow => {
    const data = initial.at(i)?.data;
    const snapshot = data?.snapshot;
    if (viewport === null || covers(data, viewport)) return { id, snapshot, range: null };
    if (ready && windows.at(i)?.data !== undefined) return { id, snapshot, range: settled };
    return shown.find((window) => window.id === id && window.snapshot === snapshot) ?? { id, snapshot, range: null };
  });
  if (shown.length !== nextShown.length || nextShown.some((window, i) => !sameWindow(window, shown[i]))) {
    setShown(nextShown);
  }
  const displayed = useQueries({
    queries: nextShown.map(({ id, snapshot, range }, i) => ({
      ...(range === null ? initialQueries[i] : windowQuery(id, snapshot, range)),
      enabled: false,
    })),
    combine: pickRead,
  });

  useEffect(() => client.getQueryCache().subscribe((event) => {
    if (event.type !== "updated" || event.action.type !== "error") return;
    const key: unknown = event.query.queryKey;
    if (!Array.isArray(key)) return;
    const kind: unknown = key[0];
    const id: unknown = key[1];
    if (!["run-timeline-window", "run-timeline-context", "run-timeline-messages"].includes(String(kind)) || typeof id !== "number" || !ids.includes(id)) return;
    const error: unknown = event.query.state.error;
    if (typeof error === "object" && error !== null && "status" in error && error.status === 409) {
      void client.invalidateQueries({ queryKey: ["run-timeline", id], exact: true });
    }
  }), [client, ids]);

  const reads = useMemo(() => initial.map((first, i) => {
    const needed = viewport !== null && !covers(first.data, viewport);
    const current = windows.at(i);
    return {
      data: displayed.at(i)?.data,
      isError: first.isError,
      preparing: !first.isError && preparing(first.failureReason),
      pending: needed && (!ready || current?.isFetching === true),
      windowError: needed && ready && current?.isError === true,
    };
  }), [displayed, initial, windows, viewport, ready]);
  const retry = (id: number) => {
    const i = ids.indexOf(id);
    const key = initial.at(i)?.data === undefined ? "run-timeline" : "run-timeline-window";
    void client.refetchQueries({ queryKey: [key, id], type: "active" });
  };
  return { reads, retry, initial };
}
