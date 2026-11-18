"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";

import { useBrowser } from "@/lib/browser/browser-provider";
import type { BackendTimelineItem, ReadingWindowResponse } from "@/lib/contracts/types";
import { readScrollMemory, type SavedScroll } from "@/lib/layout/scroll-memory";
import { CONVERSATION_RETENTION_MS } from "@/lib/layout/switch-budget";
import { useApi, useWallNow } from "@/lib/transport/transport-context";
import type { Api } from "@/lib/transport/api";
import type { OlderLoadOrigin, UseTimelineResult } from "../use-timeline";
import { readReadingBookmark, type ReadingBookmark } from "./bookmarks";
import { canonicalPosition, confirmedAnchor, mergeReadingItems, overlapsAcceptedHead, ReadingCoordinatesChangedError } from "./plan";
import type { TimelinePositionControl } from "./position-control";
import { boundedReadingRequest } from "./requests";
import { useReadingSaver } from "./use-reading-saver";

export { ReadingCoordinatesChangedError } from "./plan";

interface Visit {
  agentId: number | null;
  entryKey: string;
  bookmark: ReadingBookmark | null;
  position: SavedScroll | null;
}

type ReadingWindowSelector = Parameters<Api["getReadingWindow"]>[1];

interface Layout {
  locator: string;
  pages: ReadingWindowResponse[];
}

interface View {
  visit: Visit;
  mode: "live" | "parked";
  pages: ReadingWindowResponse[];
  qualified: boolean;
  domReady: boolean;
  loading: "anchor" | "before" | "after" | null;
  readerOlder: boolean;
  error: Error | null;
  position: SavedScroll | null;
  token: object;
  allowLiveWrite: boolean;
  continued: boolean;
}

const EMPTY_ITEMS: BackendTimelineItem[] = [];
const DOM_RESTORE_BUDGET_MS = 7000;

function initialView(visit: Visit, cached?: Layout): View {
  const locator = visit.bookmark?.locator;
  const cachedPages = cached?.pages ?? [];
  return {
    visit, mode: visit.bookmark ? "parked" : "live",
    pages: locator !== undefined && cached?.locator === locator ? cachedPages : [],
    qualified: false, domReady: !visit.bookmark, loading: visit.bookmark ? "anchor" : null,
    readerOlder: false, error: null, position: visit.position, token: {},
    allowLiveWrite: visit.bookmark === null, continued: false,
  };
}

function memoryBookmark(agentId: number, entryKey: string, now: number): ReadingBookmark | null {
  const position = readScrollMemory(entryKey);
  if (position?.contentKey !== String(agentId) || !position.anchor?.readingLocator) return null;
  return { agentId, savedAt: now, locator: position.anchor.readingLocator, position: { ...position, anchor: position.anchor } };
}

function failure(error: unknown): Error {
  return error instanceof Error ? error : new Error("The reading window could not be loaded");
}

export function useReadingWindow(agentId: number | null, entryKey: string, live: UseTimelineResult) {
  const browser = useBrowser();
  const api = useApi();
  const wallNow = useWallNow();
  const client = useQueryClient();
  const readVisit = (): Visit => {
    if (agentId === null) return { agentId, entryKey, bookmark: null, position: null };
    const stored = readReadingBookmark(browser.session, agentId, wallNow());
    const memory = readScrollMemory(entryKey);
    const position = memory?.contentKey === String(agentId) ? memory : null;
    // meiva:note
    // Same-entry geometry remains useful before its visible row has a durable
    // source locator. It wins within that entry without replacing the older
    // durable record or claiming source qualification for a local coordinate.
    const bookmark = position ? memoryBookmark(agentId, entryKey, wallNow())
      : stored.state === "ready" ? stored.bookmark : null;
    return { agentId, entryKey, bookmark, position: position ?? bookmark?.position ?? null };
  };
  const [visit, setVisit] = useState(readVisit);
  if (visit.agentId !== agentId || visit.entryKey !== entryKey) setVisit(readVisit());
  const cached = client.getQueryData<Layout>(["parked-reading-layout", agentId]);
  const [state, setState] = useState(() => initialView(visit, cached));
  if (state.visit !== visit) setState(initialView(visit, cached));
  const view = state.visit === visit ? state : initialView(visit, cached);
  const owner = useRef(visit);
  const latest = useRef(view);
  const request = useRef<AbortController | null>(null);
  const lastSelector = useRef<ReadingWindowSelector | null>(null);
  const lastVisibleLocator = useRef<string | null>(visit.bookmark?.locator ?? null);
  const lastVisiblePosition = useRef<SavedScroll | null>(visit.bookmark?.position ?? null);
  const liveRef = useRef(live);
  useLayoutEffect(() => { latest.current = view; liveRef.current = live; });
  useLayoutEffect(() => {
    owner.current = visit;
    lastVisibleLocator.current = visit.bookmark?.locator ?? null;
    lastVisiblePosition.current = visit.bookmark?.position ?? null;
    lastSelector.current = null;
    client.setQueryDefaults(["parked-reading-layout"], { gcTime: CONVERSATION_RETENTION_MS });
    return () => {
      request.current?.abort(); request.current = null;
      const value = latest.current;
      if (visit.agentId !== null && value.visit === visit && value.pages.length > 0) {
        const position = readScrollMemory(visit.entryKey);
        const locator = position?.anchor?.readingLocator ?? visit.bookmark?.locator;
        if (locator) client.setQueryData<Layout>(["parked-reading-layout", visit.agentId], { locator, pages: value.pages });
      }
    };
  }, [client, visit]);

  const acceptPage = useCallback((page: ReadingWindowResponse, selector: ReadingWindowSelector) => {
    if (!page.reading_source) throw new Error("The source did not confirm its reading window");
    if ("anchor" in selector) {
      mergeReadingItems([page]);
      const item = confirmedAnchor(page);
      const saved = lastVisiblePosition.current ?? latest.current.position ?? visit.bookmark?.position;
      if (!saved) throw new Error("The reading visit has no saved position");
      const position = canonicalPosition(saved, item);
      lastVisibleLocator.current = item.reading_locator ?? null;
      lastVisiblePosition.current = position;
      setState((value) => value.visit !== visit ? value : {
        ...value, mode: "parked", pages: [page], qualified: true, domReady: false, position, token: {}, loading: null, error: null,
      });
      return;
    }
    const edge = "before" in selector ? page.older_cursor : page.newer_cursor;
    const previous = "before" in selector ? selector.before : selector.after;
    if (edge && edge === previous) throw new Error("The reading source cursor did not progress");
    const pages = "before" in selector ? [page, ...latest.current.pages] : [...latest.current.pages, page];
    mergeReadingItems(pages);
    setState((value) => value.visit !== visit ? value : {
      ...value, pages, loading: null, error: null,
    });
  }, [visit]);

  const load = useCallback((selector: ReadingWindowSelector, readerOlder = false) => {
    if (agentId === null || owner.current !== visit || request.current) return;
    const controller = new AbortController();
    request.current = controller; lastSelector.current = selector;
    const direction = "anchor" in selector ? "anchor" : "before" in selector ? "before" : "after";
    setState((value) => value.visit !== visit ? value : {
      ...value, loading: direction, readerOlder, error: null,
      mode: direction === "anchor" ? "parked" : value.mode,
      qualified: direction === "anchor" ? false : value.qualified,
      domReady: direction === "anchor" ? false : value.domReady,
    });
    void boundedReadingRequest(controller, (signal) => api.getReadingWindow(agentId, selector, signal)).then((page) => {
      if (controller.signal.aborted || owner.current !== visit) return;
      acceptPage(page, selector);
    }).catch((error: unknown) => {
      if (owner.current !== visit || request.current !== controller) return;
      setState((value) => value.visit !== visit ? value : { ...value, loading: null, error: failure(error) });
    }).finally(() => { if (request.current === controller) request.current = null; });
  }, [acceptPage, agentId, api, visit]);

  useEffect(() => {
    if (visit.bookmark) load({ anchor: visit.bookmark.locator });
  }, [load, visit]);
  useEffect(() => {
    if (!view.qualified || view.domReady || view.error) return;
    const timer = setTimeout(() => {
      if (owner.current === visit) setState((value) => value.visit !== visit || value.domReady ? value : {
        ...value, loading: null, error: new Error("The saved reading item is not reachable yet"),
      });
    }, DOM_RESTORE_BUDGET_MS);
    return () => clearTimeout(timer);
  }, [view.domReady, view.error, view.qualified, visit]);

  const parkedItems = useMemo(() => mergeReadingItems(view.pages), [view.pages]);
  const writable = view.mode === "parked"
    ? view.qualified && view.domReady && !view.loading && !view.error
    : live.sourceReady && view.allowLiveWrite && view.domReady && !view.error;
  const saver = useReadingSaver({ agentId, entryKey, visit, enabled: writable, sourceVersion: live.sourceVersion,
    verifiedItems: view.qualified ? parkedItems : EMPTY_ITEMS, readFreshHead: live.readFreshHead });

  const navigated = useCallback(() => {
    saver.navigated();
    const value = latest.current;
    if (value.mode === "parked" && !value.qualified) { request.current?.abort(); request.current = null; }
    setState((current) => current.visit !== visit ? current : {
      ...current, mode: current.qualified ? current.mode : "live", position: null, token: {},
      domReady: true, allowLiveWrite: true, continued: !current.qualified, loading: null, error: null,
    });
  }, [saver, visit]);
  const remember = useCallback((position: SavedScroll, item: BackendTimelineItem | null) => {
    if (writable && item?.reading_locator && position.anchor) {
      lastVisibleLocator.current = item.reading_locator;
      lastVisiblePosition.current = canonicalPosition(position, item);
    }
    saver.remember(position, item);
  }, [saver, writable]);
  const restored = useCallback(() => {
    setState((value) => value.visit !== visit || value.domReady || value.error ? value : { ...value, domReady: true });
  }, [visit]);

  const continueHere = useCallback(() => {
    request.current?.abort(); request.current = null;
    setState((value) => value.visit !== visit ? value : {
      ...value, mode: "live", position: null, token: {}, continued: true, allowLiveWrite: false,
      loading: null, error: null, domReady: true,
    });
  }, [visit]);
  const cancel = useCallback(() => {
    request.current?.abort(); request.current = null;
    setState((value) => value.visit !== visit ? value : {
      ...value, position: null, token: {}, loading: null,
      error: new DOMException("Reading position restoration cancelled", "AbortError"),
    });
  }, [visit]);
  const retry = useCallback(() => {
    const pending = view.position?.anchor?.readingLocator ?? (!view.domReady ? lastVisibleLocator.current : null);
    if (view.error instanceof ReadingCoordinatesChangedError && lastVisibleLocator.current) {
      load({ anchor: lastVisibleLocator.current });
    } else if (pending && !view.domReady) load({ anchor: pending });
    else if (view.mode === "parked") {
      const selector = lastSelector.current ?? (visit.bookmark ? { anchor: visit.bookmark.locator } : null);
      if (selector) load(selector, view.readerOlder);
    } else saver.retry();
  }, [load, saver, view.domReady, view.error, view.mode, view.position, view.readerOlder, visit]);
  const loadOlder = useCallback((origin: OlderLoadOrigin) => {
    if (view.mode === "live") { live.loadOlder(origin); return; }
    if (!view.qualified || !view.domReady || view.error instanceof ReadingCoordinatesChangedError) return;
    const cursor = view.pages[0]?.older_cursor;
    if (cursor) load({ before: cursor }, origin === "reader");
  }, [live, load, view.domReady, view.error, view.mode, view.pages, view.qualified]);

  const loadNewer = useCallback(() => {
    if (agentId === null || owner.current !== visit || request.current || !view.qualified || !view.domReady || view.error) return;
    const controller = new AbortController(); request.current = controller;
    setState((value) => value.visit !== visit ? value : { ...value, loading: "after", error: null });
    void boundedReadingRequest(controller, async (signal) => {
      let pages = latest.current.pages;
      let cursor = pages.at(-1)?.newer_cursor;
      if (!cursor) {
        const edge = mergeReadingItems(pages).findLast((item) => item.reading_locator)?.reading_locator;
        if (!edge) throw new Error("The window has no stable newer source edge");
        const refreshed = await api.getReadingWindow(agentId, { anchor: edge }, signal, 1);
        confirmedAnchor(refreshed);
        cursor = refreshed.newer_cursor;
      }
      if (cursor) {
        const page = await api.getReadingWindow(agentId, { after: cursor }, signal);
        if (page.newer_cursor === cursor) throw new Error("The reading source cursor did not progress");
        if (!page.reading_source) throw new Error("The source did not confirm its reading window");
        pages = [...pages, page];
      }
      signal.throwIfAborted();
      if (owner.current !== visit) return;
      const merged = mergeReadingItems(pages);
      setState((value) => value.visit !== visit ? value : { ...value, pages });
      let joined = false;
      let joinedPosition: SavedScroll | null = null;
      if (!pages.at(-1)?.newer_cursor) {
        const receipt = await liveRef.current.readFreshHead(signal);
        signal.throwIfAborted();
        const visible = merged.filter((item) => item.reading_locator === lastVisibleLocator.current);
        const targets = receipt.data.items.filter((item) => item.reading_locator === lastVisibleLocator.current);
        const position = lastVisiblePosition.current;
        joined = receipt.accepted && targets.length === 1 && position?.anchor !== undefined &&
          overlapsAcceptedHead(visible, receipt.data);
        if (joined && position) joinedPosition = canonicalPosition(position, targets[0]);
      }
      if (owner.current !== visit || signal.aborted) return;
      setState((value) => value.visit !== visit ? value : {
        ...value, pages, mode: joined ? "live" : "parked", loading: null,
        allowLiveWrite: joined, error: null,
        position: joined ? joinedPosition : value.position, token: joined ? {} : value.token,
        domReady: joined ? false : value.domReady,
      });
    }).catch((error: unknown) => {
      if (owner.current !== visit || request.current !== controller) return;
      setState((value) => value.visit !== visit ? value : { ...value, loading: null, error: failure(error) });
    }).finally(() => { if (request.current === controller) request.current = null; });
  }, [agentId, api, view.domReady, view.error, view.qualified, visit]);

  const positionControl: TimelinePositionControl = {
    token: view.token, position: view.position,
    ready: (view.mode === "parked" ? view.qualified : live.sourceReady) && !view.error,
    writable, remember, navigated, restored,
  };
  const parked = view.mode === "parked";
  return {
    ...live,
    items: parked && view.pages.length ? parkedItems : live.items,
    compactBuffer: parked ? null : live.compactBuffer,
    streamingCode: !parked && live.streamingCode,
    sourceReady: parked ? view.qualified : live.sourceReady,
    isLoading: parked ? !view.error && (view.loading === "anchor" || !view.domReady) : !view.continued && live.isLoading,
    hasMoreOlder: parked ? view.qualified && view.domReady && Boolean(view.pages[0]?.older_cursor) : live.hasMoreOlder,
    loadingOlder: parked ? view.loading === "before" : live.loadingOlder,
    readerLoadingOlder: parked ? view.loading === "before" && view.readerOlder : live.readerLoadingOlder,
    loadOlder,
    reading: { parked, positionControl, canPage: view.qualified && view.domReady && !view.error,
      loadingNewer: view.loading === "after", loadNewer,
      live,
      error: view.error, saveError: saver.error, saveStatus: !parked && !view.allowLiveWrite ? "unsaved" as const : saver.status, retry, continueHere, cancel },
  };
}
