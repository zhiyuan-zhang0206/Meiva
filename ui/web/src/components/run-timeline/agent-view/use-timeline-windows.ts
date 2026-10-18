"use client";

import { useQueries, useQueryClient } from "@tanstack/react-query";
import { useEffect, useMemo, useRef, useState } from "react";

import type { RunTimelineResponse } from "@/lib/contracts/types";
import { api, ApiError } from "@/lib/transport/api";

import { viewportOf, type Viewport } from "../model/timeline-model";

import { completeRanges, mergeWindows, omittedIn, overlaps, padded, uncovered } from "./timeline-window-cache";

const MAX_UNITS = 5000;
const pickRead = (results: { data: RunTimelineResponse | undefined; isError: boolean; isFetching: boolean; error: Error | null; failureReason: Error | null }[]) =>
  results.map(({ data, isError, isFetching, error, failureReason }) => ({ data, isError, isFetching, error, failureReason }));

function preparing(error: Error | null): boolean {
  return error instanceof ApiError &&
    ["timeline_index_queued", "timeline_index_indexing", "timeline_index_busy"].includes(error.code ?? "");
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

interface RequestedWindow {
  id: number;
  snapshot: RunTimelineResponse["snapshot"];
  range: Viewport;
}

function sameWindow(a: RequestedWindow, b: RequestedWindow) {
  return a.id === b.id && a.snapshot === b.snapshot && a.range.from === b.range.from && a.range.to === b.range.to;
}

function useRequestView(viewport: Viewport | null) {
  const [view, setView] = useState(viewport);
  const latest = useRef(viewport);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => {
    latest.current = viewport;
    if (timer.current !== null) return;
    timer.current = setTimeout(() => {
      timer.current = null;
      setView(latest.current);
    }, 80);
  }, [viewport]);
  useEffect(() => () => {
    if (timer.current !== null) clearTimeout(timer.current);
    timer.current = null;
  }, []);
  return viewport === null ? null : view;
}

// meiva:link ui/web/src/docs/frontend-components/run-timeline/window-reads.meiva.okf.md
export function useTimelineWindows(ids: readonly number[], viewport: Viewport | null) {
  const client = useQueryClient();
  const requestView = useRequestView(viewport);
  const initialQueries = ids.map((id) => ({
    queryKey: ["run-timeline", id],
    queryFn: ({ signal }: { signal: AbortSignal }) => api.getRunTimeline(id, { recentHours: 24, maxUnits: MAX_UNITS, signal }),
    staleTime: Infinity,
    gcTime: 60_000,
    retry: (attempt: number, error: Error) => preparing(error) && attempt < 30,
    retryDelay: 2_000,
  }));
  const initial = useQueries({ queries: initialQueries, combine: pickRead });
  const [requested, setRequested] = useState<RequestedWindow[]>([]);
  const active = useMemo(() => requested.filter((item) => {
    const index = ids.indexOf(item.id);
    const first = index < 0 ? undefined : initial[index]?.data;
    return first !== undefined && first.snapshot === item.snapshot;
  }), [requested, ids, initial]);
  const windows = useQueries({ queries: active.map(({ id, snapshot, range }) => windowQuery(id, snapshot, range)), combine: pickRead });
  const next = [...active];
  initial.forEach(({ data: first }, i) => {
    if (first === undefined || requestView === null || first.lifetime === null) return;
    const lifetime = viewportOf(first.lifetime);
    const visible = padded(requestView, lifetime, 0);
    if (visible.from >= visible.to) return;
    const mine = active.map((item, index) => ({ ...item, read: windows[index] })).filter((item) => item.id === ids[i]);
    const data = [first, ...mine.flatMap(({ read }) => read.data === undefined ? [] : [read.data])];
    const complete = completeRanges(data);
    const attempted = mine.filter(({ read }) => read.data === undefined).map(({ range }) => range);
    const dense = data.some((item) => item.units_omitted > 0 && overlaps(viewportOf(item.window), visible));
    const target = dense ? visible : padded(visible, lifetime, 1);
    const covered = [...complete, ...attempted];
    if (uncovered(dense ? visible : padded(visible, lifetime, 0.25), covered).length === 0) return;
    const ranges = uncovered(target, covered);
    for (const range of ranges) {
      const candidate = { id: ids[i], snapshot: first.snapshot, range };
      if (!next.some((item) => sameWindow(candidate, item))) next.push(candidate);
    }
  });
  if (next.length !== requested.length || next.some((item, i) => !sameWindow(item, requested[i]))) setRequested(next);

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

  const collections = useMemo(() => initial.map((first, i) => {
    const mine = active.map((item, index) => ({ ...item, read: windows[index] })).filter((item) => item.id === ids[i]);
    const data = [first.data, ...mine.map(({ read }) => read.data)].filter((item): item is RunTimelineResponse => item !== undefined);
    const merged = mergeWindows(data, null);
    const counts = new Set([0, ...data.map((item) => item.units_omitted)]);
    const displays = merged === undefined ? [] : [...counts].map((count) => count === merged.units_omitted ? merged : { ...merged, units_omitted: count });
    return { first, mine, data, merged, displays };
  }), [active, ids, initial, windows]);
  const reads = useMemo(() => collections.map(({ first, mine, data, merged, displays }) => {
    const visible = viewport === null || first.data?.lifetime == null ? null : padded(viewport, viewportOf(first.data.lifetime), 0);
    const needed = visible !== null && uncovered(visible, completeRanges(data)).length > 0;
    const relevant = mine.filter(({ range }) => visible !== null && overlaps(range, visible));
    const failed = relevant.some(({ read }) => read.isError);
    const omitted = data.some((item) => item.units_omitted > 0 && visible !== null && overlaps(viewportOf(item.window), visible));
    const count = visible === null ? merged?.units_omitted : omittedIn(data, visible);
    return {
      data: displays.find((item) => item.units_omitted === count),
      isError: first.isError,
      preparing: !first.isError && preparing(first.failureReason),
      pending: needed && (relevant.some(({ read }) => read.isFetching) || (!failed && !omitted)),
      windowError: needed && failed,
    };
  }), [collections, viewport]);
  const retry = (id: number) => {
    const i = ids.indexOf(id);
    const key = initial.at(i)?.data === undefined ? "run-timeline" : "run-timeline-window";
    void client.refetchQueries({ queryKey: [key, id], type: "active", predicate: (query) => query.state.status === "error" });
  };
  return { reads, retry, initial };
}
