"use client";

import { useCallback, useLayoutEffect, useRef, useState } from "react";

import { useBrowser } from "@/lib/browser/browser-provider";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { saveScrollMemory, type SavedScroll } from "@/lib/layout/scroll-memory";
import { useApi, useWallNow } from "@/lib/transport/transport-context";
import type { UseTimelineResult } from "../use-timeline";
import { saveReadingBookmark } from "./bookmarks";
import { canonicalPosition, type ReadingCandidate } from "./plan";
import { candidateKey } from "./identity";
import { boundedReadingRequest, confirmLiveReadingCandidate } from "./requests";

interface Args {
  agentId: number | null;
  entryKey: string;
  visit: object;
  enabled: boolean;
  sourceVersion: number;
  verifiedItems: readonly BackendTimelineItem[];
  readFreshHead: UseTimelineResult["readFreshHead"];
}

interface Pending extends ReadingCandidate {
  visit: object;
}

type SaveStatus = "idle" | "pending" | "saved" | "unsaved";

export function useReadingSaver({
  agentId, entryKey, visit, enabled, sourceVersion, verifiedItems, readFreshHead,
}: Args) {
  const browser = useBrowser();
  const api = useApi();
  const wallNow = useWallNow();
  const owner = useRef(visit);
  const allowed = useRef(enabled);
  const latest = useRef<Pending | null>(null);
  const request = useRef<AbortController | null>(null);
  const verified = useRef(new Map<string, BackendTimelineItem>());
  const attempted = useRef<string | null>(null);
  const navigation = useRef(0);
  const lastPersistAttempt = useRef<string | null>(null);
  const writeFailureAt = useRef<number | null>(null);
  const [feedback, setFeedback] = useState<{ visit: object; status: SaveStatus; error: Error | null }>({ visit, status: "idle", error: null });
  if (feedback.visit !== visit) setFeedback({ visit, status: "idle", error: null });
  const report = useCallback((status: SaveStatus, error: Error | null = null) => {
    setFeedback((value) => value.visit !== visit || (value.status === status && value.error?.name === error?.name && value.error?.message === error?.message)
      ? value : { visit, status, error });
  }, [visit]);

  useLayoutEffect(() => {
    owner.current = visit;
    verified.current = new Map();
    latest.current = null;
    attempted.current = null;
    lastPersistAttempt.current = null;
    writeFailureAt.current = null;
    return () => { request.current?.abort(); request.current = null; };
  }, [visit]);
  useLayoutEffect(() => {
    allowed.current = enabled;
    if (!enabled) { request.current?.abort(); request.current = null; }
  }, [enabled]);
  useLayoutEffect(() => {
    for (const item of verifiedItems) if (item.reading_locator) verified.current.set(item.reading_locator, item);
  }, [verifiedItems]);

  const persist = useCallback((candidate: Pending, item: BackendTimelineItem) => {
    if (owner.current !== candidate.visit || !allowed.current || agentId === null) return;
    const position = canonicalPosition(candidate.position, item);
    if (!position.anchor || !item.reading_locator) return;
    if (writeFailureAt.current !== null && navigation.current <= writeFailureAt.current) return;
    const signature = JSON.stringify([item.reading_locator, position, navigation.current]);
    if (lastPersistAttempt.current === signature) return;
    lastPersistAttempt.current = signature;
    const result = saveReadingBookmark(browser.session, {
      agentId, savedAt: wallNow(), locator: item.reading_locator,
      position: { ...position, anchor: position.anchor },
    }, wallNow());
    if (result === "stored") {
      saveScrollMemory(entryKey, position);
      report("saved");
    } else {
      writeFailureAt.current = navigation.current;
      report("unsaved", new Error("The browser could not save this reading position"));
    }
  }, [agentId, browser.session, entryKey, report, wallNow]);

  const remember = useCallback((position: SavedScroll, item: BackendTimelineItem | null) => {
    if (!enabled || !allowed.current || agentId === null || !position.anchor || !item) return;
    const key = candidateKey(item) ?? (!item.partial && item.item_id.startsWith("h") ? `unbound:${item.item_id}` : null);
    if (!key) {
      request.current?.abort(); request.current = null; latest.current = null;
      report(item.partial ? "pending" : "unsaved", item.partial ? null : new Error("The visible item has no restorable source identity"));
      return;
    }
    const previous = latest.current;
    if (previous?.key !== key) { request.current?.abort(); request.current = null; }
    const candidate: Pending = { key, item, position, visit };
    latest.current = candidate;
    const confirmed = verified.current.get(key);
    if (confirmed) { persist(candidate, confirmed); return; }
    const attempt = JSON.stringify([key, sourceVersion, navigation.current]);
    if (request.current || attempted.current === attempt) return;
    attempted.current = attempt;
    const controller = new AbortController();
    request.current = controller;
    report("pending");
    void boundedReadingRequest(controller, async (signal) => {
      if (key.startsWith("unbound:")) {
        const head = await readFreshHead(signal);
        if (!head.accepted) throw new Error("The fresh source could not be applied");
        return null;
      }
      return confirmLiveReadingCandidate(api, agentId, item, signal);
    }).then((bound) => {
      if (controller.signal.aborted || owner.current !== visit || latest.current?.key !== key || !allowed.current) return;
      if (bound === null) {
        report("unsaved", new Error("The visible item is waiting for a restorable source identity"));
        return;
      }
      verified.current.set(key, bound);
      if (bound.reading_locator) verified.current.set(bound.reading_locator, bound);
      persist(latest.current, bound);
    }).catch((failure: unknown) => {
      if (owner.current !== visit || latest.current?.key !== key || !allowed.current || request.current !== controller) return;
      report("unsaved", failure instanceof Error ? failure : new Error("Reading position confirmation failed"));
    }).finally(() => { if (request.current === controller) request.current = null; });
  }, [agentId, api, enabled, persist, readFreshHead, report, sourceVersion, visit]);

  const navigated = useCallback(() => {
    navigation.current += 1; attempted.current = null; writeFailureAt.current = null;
  }, []);
  const retry = useCallback(() => {
    navigation.current += 1; attempted.current = null; writeFailureAt.current = null;
    const candidate = latest.current;
    if (candidate?.visit === visit) { remember(candidate.position, candidate.item); return; }
    if (!enabled || request.current) return;
    const controller = new AbortController(); request.current = controller;
    report("pending");
    void boundedReadingRequest(controller, (signal) => readFreshHead(signal)).then((receipt) => {
      if (owner.current !== visit || request.current !== controller) return;
      if (!receipt.accepted) throw new Error("The fresh source could not be applied");
      report("unsaved", new Error("The visible item is waiting for a restorable source identity"));
    }).catch((error: unknown) => {
      if (owner.current === visit && request.current === controller) report("unsaved", error instanceof Error ? error : new Error("Reading position confirmation failed"));
    }).finally(() => { if (request.current === controller) request.current = null; });
  }, [enabled, readFreshHead, remember, report, visit]);
  return { remember, navigated, retry, status: feedback.status, error: feedback.error };
}
