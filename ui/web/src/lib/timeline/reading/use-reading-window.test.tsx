import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it } from "vitest";

import { BrowserProvider } from "@/lib/browser/browser-provider";
import { fakeBrowser } from "@/lib/browser/testing";
import type { BackendTimelineItem, ReadingWindowResponse, TimelineResponse } from "@/lib/contracts/types";
import { httpRig, noUpload, sequentialKeys } from "@/lib/transport/tests/api/api-provider";
import { ApiProvider, WallClockProvider } from "@/lib/transport/transport-context";
import type { UseTimelineResult } from "../use-timeline";
import { saveScrollMemory } from "@/lib/layout/scroll-memory";
import { readReadingBookmark, READING_BOOKMARKS_KEY, saveReadingBookmark } from "./bookmarks";
import { ReadingCoordinatesChangedError, useReadingWindow } from "./use-reading-window";

function item(id = "2.0", locator: string | null = "saved"): BackendTimelineItem {
  return { item_id: id, kind: "agent_chat", payload: "source text", source: null, created_at: null,
    inbound_id: null, source_message_id: `message_${id}`, source_block_idx: 0, reading_locator: locator, show_timestamp: true };
}

function position(id = "2.0", offset = 97) {
  return { contentKey: "1", scrollTop: 600, followBottom: false,
    anchor: { itemId: id, rank: 0, viewportTop: offset } };
}

function page(row = item()): ReadingWindowResponse {
  return { items: [row], reading_source: "source_domain", anchor_item_id: row.item_id,
    older_cursor: null, newer_cursor: null };
}

function requestUrl(input: RequestInfo | URL): string {
  return typeof input === "string" ? input : "url" in input ? input.url : input.href;
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });
}

function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((done) => { resolve = done; });
  return { promise, resolve };
}

function live(items: BackendTimelineItem[] = []): UseTimelineResult {
  return { items, compactBuffer: null, streamingCode: false, connectionState: "open", turnActive: true,
    sourceReady: true, sourceVersion: 1, isLoading: false, isRefetching: false, isFetching: false,
    error: null, loadingNewer: false, retryTimeline: () => undefined,
    readFreshHead: () => Promise.resolve({ data: { items, has_more: false, msg_count: items.length }, accepted: true }),
    hasMoreOlder: false, loadingOlder: false, readerLoadingOlder: false, loadOlder: () => undefined, retainedItemsMax: 250 };
}

function setup(fetch: (...args: Parameters<typeof globalThis.fetch>) => Response | Promise<Response>, saved = true) {
  const browser = fakeBrowser({ session: {}, local: {} });
  if (saved) saveReadingBookmark(browser.session, { agentId: 1, savedAt: 1000, locator: "saved", position: position() }, 1000);
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  caches.push(cache);
  const rig = httpRig({ base: "http://reading.test", fetch: (...args) => Promise.resolve(fetch(...args)), now: () => 0, keys: sequentialKeys(), openUpload: noUpload });
  const wrapper = ({ children }: { children: ReactNode }) => <BrowserProvider browser={browser}>
    <QueryClientProvider client={cache}><ApiProvider api={rig.api}><WallClockProvider wallNow={() => 1001}>{children}</WallClockProvider></ApiProvider></QueryClientProvider>
  </BrowserProvider>;
  return { browser, cache, rig, wrapper };
}

const caches: QueryClient[] = [];
afterEach(() => { cleanup(); for (const cache of caches) cache.clear(); caches.length = 0; });

describe("parked reading visits", () => {
  it("uses the source before cursor and retries a refused page only after another explicit pull", async () => {
    let pulls = 0;
    const initial = { ...page(), older_cursor: "older_edge" };
    const context = setup((input) => {
      const url = new URL(requestUrl(input));
      if (!url.searchParams.has("before")) return json(initial);
      pulls += 1;
      expect(url.pathname).toBe("/api/agents/1/timeline/window");
      expect(url.searchParams.get("before")).toBe("older_edge");
      return pulls === 1 ? json({ detail: "Checkpoint history unavailable" }, 503)
        : json({ ...page(item("1.0", "older")), anchor_item_id: null });
    });
    const durable = context.browser.sessionValues.get(READING_BOOKMARKS_KEY);
    const view = renderHook(() => useReadingWindow(1, "explicit-older-retry", live()), { wrapper: context.wrapper });
    await waitFor(() => expect(view.result.current.sourceReady).toBe(true));
    act(() => view.result.current.reading.positionControl.restored());
    act(() => view.result.current.loadOlder("reader"));
    await waitFor(() => expect(view.result.current.reading.error).not.toBeNull());
    expect(view.result.current.items.map((row) => row.item_id)).toEqual(["2.0"]);
    expect(view.result.current.loadingOlder).toBe(false);
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(durable);
    expect(pulls).toBe(1);
    act(() => view.result.current.loadOlder("reader"));
    await waitFor(() => expect(view.result.current.items.map((row) => row.item_id)).toEqual(["1.0", "2.0"]));
    expect(pulls).toBe(2);
    expect(view.result.current.reading.error).toBeNull();
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(durable);
    view.unmount();
  });

  it("keeps newer same-entry geometry ahead of an older durable bookmark without qualifying cached source", () => {
    const context = setup(() => json(page()));
    const durable = context.browser.sessionValues.get(READING_BOOKMARKS_KEY);
    const entry = "new-local-old-durable";
    saveScrollMemory(entry, position("3.0", -100));
    const source = live([item("3.0", null)]);
    source.sourceReady = false;
    const view = renderHook(() => useReadingWindow(1, entry, source), { wrapper: context.wrapper });
    expect(view.result.current.reading.parked).toBe(false);
    expect(view.result.current.reading.positionControl.position?.anchor?.itemId).toBe("3.0");
    expect(view.result.current.reading.positionControl.ready).toBe(false);
    expect(view.result.current.reading.positionControl.writable).toBe(false);
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(durable);
    expect(context.rig.sent).toHaveLength(0);
    view.unmount();
  });

  it("separates source qualification from actual viewport completion and does not save a fallback", async () => {
    const context = setup(() => json(page()));
    const original = context.browser.sessionValues.get(READING_BOOKMARKS_KEY);
    const view = renderHook(() => useReadingWindow(1, "source_dom", live()), { wrapper: context.wrapper });
    await waitFor(() => expect(view.result.current.sourceReady).toBe(true));
    expect(view.result.current.isLoading).toBe(true);
    expect(view.result.current.reading.positionControl.writable).toBe(false);
    act(() => view.result.current.reading.positionControl.remember(position(), item()));
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(original);
    act(() => view.result.current.reading.positionControl.restored());
    expect(view.result.current.isLoading).toBe(false);
    expect(view.result.current.reading.positionControl.writable).toBe(true);
    view.unmount();
  });

  it("keeps a cached layout unqualified on A2 and rejects late A1 even when cancellation is ignored", async () => {
    const first = deferred<Response>();
    let reads = 0;
    const context = setup(() => ++reads === 1 ? first.promise : json(page(item("s1.cp.0.0", "canonical"))));
    const view = renderHook(({ id }) => useReadingWindow(id, `race_agent_${id}`, live()), {
      wrapper: context.wrapper, initialProps: { id: 1 },
    });
    await waitFor(() => expect(reads).toBe(1));
    act(() => view.rerender({ id: 2 }));
    act(() => view.rerender({ id: 1 }));
    await waitFor(() => expect(view.result.current.sourceReady).toBe(true));
    expect(view.result.current.reading.positionControl.position?.anchor?.itemId).toBe("s1.cp.0.0");
    expect(context.rig.sent[0].init.signal?.aborted).toBe(true);
    await act(async () => { first.resolve(json(page(item("wrong.0", "late")))); await Promise.resolve(); });
    expect(view.result.current.reading.positionControl.position?.anchor?.itemId).toBe("s1.cp.0.0");
    view.unmount();
  });

  it("re-requests a cached parked window and keeps the bookmark on source refusal", async () => {
    const revisit = deferred<Response>();
    let reads = 0;
    const context = setup(() => ++reads === 1 ? json(page()) : revisit.promise);
    const original = context.browser.sessionValues.get(READING_BOOKMARKS_KEY);
    const view = renderHook(({ id }) => useReadingWindow(id, `cached_agent_${id}`, live()), {
      wrapper: context.wrapper, initialProps: { id: 1 },
    });
    await waitFor(() => expect(view.result.current.sourceReady).toBe(true));
    act(() => view.result.current.reading.positionControl.restored());
    act(() => view.rerender({ id: 2 }));
    act(() => view.rerender({ id: 1 }));
    expect(view.result.current.items).toEqual([item()]);
    expect(view.result.current.sourceReady).toBe(false);
    expect(view.result.current.reading.positionControl.writable).toBe(false);
    act(() => { revisit.resolve(json({ code: "reading_source_unavailable", reading_reason: "source" }, 410)); });
    await waitFor(() => expect(view.result.current.reading.error).not.toBeNull());
    expect(view.result.current.isLoading).toBe(false);
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(original);
    view.unmount();
  });

  it("cancels a hanging restore and Continue leaves the old durable record protected", async () => {
    const context = setup(() => new Promise<Response>(() => undefined));
    const original = context.browser.sessionValues.get(READING_BOOKMARKS_KEY);
    const view = renderHook(() => useReadingWindow(1, "cancel_restore", live()), { wrapper: context.wrapper });
    await waitFor(() => expect(context.rig.sent).toHaveLength(1));
    act(() => view.result.current.reading.cancel());
    expect(view.result.current.isLoading).toBe(false);
    expect(context.rig.sent[0].init.signal?.aborted).toBe(true);
    act(() => view.result.current.reading.continueHere());
    expect(view.result.current.reading.parked).toBe(false);
    expect(view.result.current.reading.saveStatus).toBe("unsaved");
    expect(view.result.current.reading.positionControl.writable).toBe(false);
    act(() => view.result.current.reading.positionControl.remember(position(), item()));
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(original);
    view.unmount();
  });

  it("stops DOM restoration after cancellation even when the source already qualified", async () => {
    const context = setup(() => json(page()));
    const original = context.browser.sessionValues.get(READING_BOOKMARKS_KEY);
    const view = renderHook(() => useReadingWindow(1, "cancel_qualified_dom", live()), { wrapper: context.wrapper });
    await waitFor(() => expect(view.result.current.sourceReady).toBe(true));
    expect(view.result.current.isLoading).toBe(true);
    act(() => view.result.current.reading.cancel());
    expect(view.result.current.isLoading).toBe(false);
    expect(view.result.current.reading.positionControl.ready).toBe(false);
    expect(view.result.current.reading.positionControl.writable).toBe(false);
    act(() => view.result.current.reading.positionControl.restored());
    expect(view.result.current.reading.error?.name).toBe("AbortError");
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(original);
    act(() => view.result.current.reading.retry());
    await waitFor(() => expect(context.rig.sent).toHaveLength(2));
    await waitFor(() => expect(view.result.current.reading.positionControl.ready).toBe(true));
    expect(view.result.current.isLoading).toBe(true);
    act(() => view.result.current.reading.positionControl.restored());
    expect(view.result.current.isLoading).toBe(false);
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(original);
    view.unmount();
  });

  it("keeps historical rows independent of live replacement and compaction buffers", async () => {
    const context = setup(() => json(page()));
    const view = renderHook(({ current }) => useReadingWindow(1, "independent_source", current), {
      wrapper: context.wrapper, initialProps: { current: live() },
    });
    await waitFor(() => expect(view.result.current.sourceReady).toBe(true));
    const current = live([item("9.0", "new_live")]);
    act(() => view.rerender({ current }));
    expect(view.result.current.items).toEqual([item()]);
    expect(view.result.current.compactBuffer).toBeNull();
    expect(view.result.current.turnActive).toBe(true);
    view.unmount();
  });
});

describe("live reading persistence", () => {
  it("keeps one inflight confirmation and saves the latest measured offset of that candidate", async () => {
    const head = deferred<Response>();
    const source = item("2.0", "canonical");
    const context = setup((url) => requestUrl(url).includes("/window?") ? json(page(source)) : head.promise, false);
    const view = renderHook(() => useReadingWindow(1, "latest_offset", live([item("2.0", null)])), { wrapper: context.wrapper });
    act(() => view.result.current.reading.positionControl.remember(position("2.0", 97), item("2.0", null)));
    act(() => view.result.current.reading.positionControl.remember(position("2.0", 123), item("2.0", null)));
    await waitFor(() => expect(context.rig.sent).toHaveLength(1));
    act(() => { head.resolve(json({ items: [source], has_more: false, msg_count: 3 } satisfies TimelineResponse)); });
    await waitFor(() => expect(view.result.current.reading.saveStatus).toBe("saved"));
    const saved = readReadingBookmark(context.browser.session, 1, 1001);
    expect(saved.state === "ready" && saved.bookmark?.position.anchor.viewportTop).toBe(123);
    expect(context.rig.sent).toHaveLength(2);
    view.unmount();
  });

  it("does not let a cancelled candidate A confirmation replace the newer B position", async () => {
    const a = deferred<Response>();
    const b = item("3.0", "bound_b");
    let heads = 0;
    const context = setup((url) => requestUrl(url).includes("/window?") ? json(page(b)) :
      ++heads === 1 ? a.promise : json({ items: [b], has_more: false, msg_count: 4 }), false);
    const view = renderHook(() => useReadingWindow(1, "candidate_race", live()), { wrapper: context.wrapper });
    act(() => view.result.current.reading.positionControl.remember(position("2.0"), item("2.0", null)));
    await waitFor(() => expect(heads).toBe(1));
    act(() => view.result.current.reading.positionControl.remember(position("3.0", 101), item("3.0", null)));
    await waitFor(() => expect(view.result.current.reading.saveStatus).toBe("saved"));
    await act(async () => { a.resolve(json({ items: [item("2.0", "bound_a")] })); await Promise.resolve(); });
    const saved = readReadingBookmark(context.browser.session, 1, 1001);
    expect(saved.state === "ready" && saved.bookmark?.locator).toBe("bound_b");
    expect(context.rig.sent.filter((request) => request.url.includes("/window?"))).toHaveLength(1);
    view.unmount();
  });

  it("keeps a rejected storage write blocked through automatic geometry until real navigation", async () => {
    const source = item("3.0", "latest");
    const context = setup(() => json(page(source)), false);
    const view = renderHook(() => useReadingWindow(1, "storage_failure", live([source])), { wrapper: context.wrapper });
    context.browser.failStorage("session", "write");
    act(() => view.result.current.reading.positionControl.remember(position("3.0"), source));
    await waitFor(() => expect(view.result.current.reading.saveStatus).toBe("unsaved"));
    context.browser.failStorage("session", "remove");
    act(() => view.result.current.reading.positionControl.remember(position("3.0", 101), source));
    expect(context.browser.sessionValues.has(READING_BOOKMARKS_KEY)).toBe(false);
    act(() => view.result.current.reading.positionControl.navigated());
    act(() => view.result.current.reading.positionControl.remember(position("3.0", 103), source));
    await waitFor(() => expect(view.result.current.reading.saveStatus).toBe("saved"));
    const saved = readReadingBookmark(context.browser.session, 1, 1001);
    expect(saved.state === "ready" && saved.bookmark?.position.anchor.viewportTop).toBe(103);
    view.unmount();
  });
});

describe("newer source paging and tail join", () => {
  it("preserves the parked source when a newer native domain reuses a canonical coordinate", async () => {
    const old = item();
    const newer = { ...item("2.0", "new_current"), source_message_id: "different_native_message" };
    const context = setup((url) => {
      if (new URL(requestUrl(url)).searchParams.has("after")) return json({
        ...page(newer), reading_source: "new_native_domain", anchor_item_id: null,
      });
      return json({ ...page(old), newer_cursor: "source_newer_cursor" });
    });
    const original = context.browser.sessionValues.get(READING_BOOKMARKS_KEY);
    const view = renderHook(() => useReadingWindow(1, "native_domain_collision", live([newer])), { wrapper: context.wrapper });
    await waitFor(() => expect(view.result.current.sourceReady).toBe(true));
    act(() => view.result.current.reading.positionControl.restored());
    act(() => view.result.current.reading.loadNewer());
    await waitFor(() => expect(view.result.current.reading.loadingNewer).toBe(false));
    expect(view.result.current.reading.error).not.toBeNull();
    expect(view.result.current.items).toEqual([old]);
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(original);
    view.unmount();
  });

  it("reanchors the last measured member rather than the initial bookmark, then pages from its new canonical prefix", async () => {
    const initial = item();
    const visible = item("3.0", "visible_saved");
    const retained = { ...item("s1.cp.3.0", "retained_saved"), source_message_id: visible.source_message_id };
    const newer = { ...item("3.0", "new_current"), source_message_id: "different_native_message" };
    const context = setup((url) => {
      const query = new URL(requestUrl(url)).searchParams;
      if (query.has("after")) return json({ ...page(newer), reading_source: "new_native_domain", anchor_item_id: null });
      if (query.get("anchor") === "visible_saved") return json({
        ...page(retained), reading_source: "retained_domain", newer_cursor: "fresh_source_cursor",
      });
      return json({ ...page(initial), items: [initial, visible], newer_cursor: "source_newer_cursor" });
    });
    const view = renderHook(() => useReadingWindow(1, "native_domain_retry_visible", live([newer])), { wrapper: context.wrapper });
    await waitFor(() => expect(view.result.current.sourceReady).toBe(true));
    act(() => view.result.current.reading.positionControl.restored());
    act(() => view.result.current.reading.positionControl.navigated());
    act(() => view.result.current.reading.positionControl.remember(position("3.0", 123), visible));
    const original = context.browser.sessionValues.get(READING_BOOKMARKS_KEY);
    act(() => view.result.current.reading.loadNewer());
    await waitFor(() => expect(view.result.current.reading.error).toBeInstanceOf(ReadingCoordinatesChangedError));
    expect(view.result.current.items).toEqual([initial, visible]);
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(original);
    act(() => view.result.current.reading.retry());
    await waitFor(() => expect(view.result.current.reading.positionControl.position?.anchor?.itemId).toBe("s1.cp.3.0"));
    expect(view.result.current.reading.positionControl.position?.anchor?.viewportTop).toBe(123);
    expect(view.result.current.items).toEqual([retained]);
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBe(original);
    expect(new URL(context.rig.sent.at(-1)?.url ?? "http://fixture").searchParams.get("anchor")).toBe("visible_saved");
    act(() => view.result.current.reading.positionControl.restored());
    act(() => view.result.current.reading.loadNewer());
    await waitFor(() => expect(view.result.current.reading.loadingNewer).toBe(false));
    expect(view.result.current.reading.error).toBeNull();
    expect(view.result.current.items).toEqual([retained, newer]);
    expect(context.rig.sent.map((request) => new URL(request.url).searchParams.get("after")).filter(Boolean)).toEqual([
      "source_newer_cursor", "fresh_source_cursor",
    ]);
    view.unmount();
  });

  it("requires an actual fresh-head apply receipt despite matching source overlap", async () => {
    const source = item();
    const context = setup(() => json(page(source)));
    let accepted = false;
    const current = live([source]);
    current.readFreshHead = () => Promise.resolve({ data: { items: [source], has_more: false, msg_count: 3 }, accepted });
    const view = renderHook(() => useReadingWindow(1, "join_receipt", current), { wrapper: context.wrapper });
    await waitFor(() => expect(view.result.current.sourceReady).toBe(true));
    act(() => view.result.current.reading.positionControl.restored());
    act(() => view.result.current.reading.loadNewer());
    await waitFor(() => expect(view.result.current.reading.loadingNewer).toBe(false));
    expect(view.result.current.reading.parked).toBe(true);
    accepted = true;
    act(() => view.result.current.reading.loadNewer());
    await waitFor(() => expect(view.result.current.reading.parked).toBe(false));
    expect(view.result.current.reading.positionControl.position?.anchor?.viewportTop).toBe(97);
    expect(view.result.current.reading.positionControl.writable).toBe(false);
    act(() => view.result.current.reading.cancel());
    expect(view.result.current.reading.positionControl.ready).toBe(false);
    act(() => view.result.current.reading.retry());
    await waitFor(() => expect(view.result.current.reading.parked).toBe(true));
    await waitFor(() => expect(view.result.current.reading.positionControl.ready).toBe(true));
    act(() => view.result.current.reading.positionControl.restored());
    expect(view.result.current.reading.positionControl.writable).toBe(true);
    view.unmount();
  });

  it("refreshes a proven terminal edge to obtain a new source cursor after a live burst", async () => {
    const old = item();
    const newer = item("3.0", "newer");
    const context = setup((url) => {
      const path = new URL(requestUrl(url));
      if (path.searchParams.has("after")) return json({ ...page(newer), anchor_item_id: null });
      if (path.searchParams.get("limit") === "1") return json({ ...page(old), newer_cursor: old.reading_locator });
      return json(page(old));
    });
    const current = live([old, newer]);
    const view = renderHook(() => useReadingWindow(1, "live_burst", current), { wrapper: context.wrapper });
    await waitFor(() => expect(view.result.current.sourceReady).toBe(true));
    act(() => view.result.current.reading.positionControl.restored());
    act(() => view.result.current.reading.loadNewer());
    await waitFor(() => expect(view.result.current.reading.parked).toBe(false));
    expect(context.rig.sent.map((request) => new URL(request.url).search)).toEqual([
      "?anchor=saved", "?anchor=saved&limit=1", "?after=saved",
    ]);
    view.unmount();
  });
});
