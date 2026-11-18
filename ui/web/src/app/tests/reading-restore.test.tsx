import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { useLayoutEffect, useRef, useState, type ComponentProps, type ReactNode, type RefObject } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { TimelineView } from "@/components/timeline";
import { fakeBrowser } from "@/lib/browser/testing";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem, type ReadingWindowResponse, type TimelineResponse } from "@/lib/contracts/types";
import { READING_BOOKMARKS_KEY, readReadingBookmark, saveReadingBookmark } from "@/lib/timeline/reading/testing";
import { useReadingWindow } from "@/lib/timeline/reading/use-reading-window";
import { useTimeline } from "@/lib/timeline/use-timeline";
import { useTimelineStore } from "@/lib/timeline/timeline-store";
import { ApiProvider, StreamHubProvider, WallClockProvider } from "@/lib/transport/transport-context";
import { httpRig, noUpload, sequentialKeys } from "@/lib/transport/tests/api/api-provider";
import { fakeStreamHub, TestStreamProvider } from "@/test-support/fake-stream";
import { renderWithBrowser } from "@/test-support/render";
import { timelineGeometry } from "@/test-support/timeline-geometry";
import { cleanupHomePageTest, homePageProps, makeAgent, resetHomePageTestState } from "../page-test-mock";
import { HomePageView } from "../home-view";

const callbacks = new Set<() => void>();
const clients: QueryClient[] = [];
let fixture = 0;

function readingGeometry(initial: BackendTimelineItem[]) {
  let top = 0;
  let rows = initial;
  return {
    ...timelineGeometry({
      top: () => top, setTop: (value) => { top = value; },
      height: () => rows.length * 300 + 300, resizeCallbacks: callbacks,
      itemTop: (id) => rows.findIndex((item) => item.item_id === id) * 300,
    }),
    setRows: (items: BackendTimelineItem[]) => { rows = items; },
  };
}

function SyncGeometry({ rootRef, items, geometry }: {
  rootRef: RefObject<HTMLDivElement | null>;
  items: BackendTimelineItem[];
  geometry: ReturnType<typeof readingGeometry>;
}) {
  useLayoutEffect(() => {
    geometry.setRows(items);
    if (rootRef.current) geometry.prepare(rootRef.current);
  });
  return null;
}

function MeasuredTimeline(props: ComponentProps<typeof TimelineView>) {
  const root = useRef<HTMLDivElement>(null);
  const [geometry] = useState(() => readingGeometry(props.items));
  useLayoutEffect(() => { geometry.setRows(props.items); if (root.current) geometry.prepare(root.current); });
  return (
    <div ref={root}>
      <SyncGeometry rootRef={root} items={props.items} geometry={geometry} />
      <TimelineView {...props} viewportObserverFactory={geometry.observer} />
    </div>
  );
}

function resize() { act(() => { callbacks.forEach((deliver) => deliver()); }); }

function setup({ olderCursor = null, failBefore = false }: { olderCursor?: string | null; failBefore?: boolean } = {}) {
  const entry = `home-reading-${++fixture}`;
  const rows: BackendTimelineItem[] = Array.from({ length: 16 }, (_, index) => ({
    item_id: `${index + 1}.0`, kind: "agent_chat", payload: `Source ${index + 1}`,
    source: null, created_at: null, inbound_id: null, show_timestamp: false,
    source_message_id: `message_${index}`, source_block_idx: 0, reading_locator: `locator_${index}`,
  }));
  const browser = fakeBrowser({ session: {}, local: {} });
  saveReadingBookmark(browser.session, { agentId: 42, savedAt: 1000, locator: "locator_5", position: {
    contentKey: "42", scrollTop: 1403, followBottom: false,
    anchor: { itemId: "6.0", rank: 0, viewportTop: 97 },
  } }, 1000);
  const cache = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: Infinity } } });
  clients.push(cache);
  cache.setQueryData(["config", null], { fields: [] });
  cache.setQueryData(["user-settings"], { ...USER_SETTING_DEFAULTS, "behavior.replySuggestions": false });
  const state: { head: BackendTimelineItem[]; failBefore: boolean; reading?: ReturnType<typeof useReadingWindow> } = {
    head: rows.slice(4), failBefore,
  };
  const timeline = (): TimelineResponse => ({ items: state.head, has_more: false, msg_count: 16 });
  const rig = httpRig({ base: "http://home-reading.test", now: () => 0, keys: sequentialKeys(), openUpload: noUpload,
    fetch: (input) => {
      const url = new URL(typeof input === "string" ? input : "url" in input ? input.url : input.href);
      const path = url.pathname;
      let body: unknown;
      if (path.endsWith("/timeline/window")) {
        if (url.searchParams.get("before")) {
          if (state.failBefore) {
            return Promise.resolve(new Response(JSON.stringify({ detail: "Checkpoint history unavailable" }), {
              status: 503, headers: { "content-type": "application/json" },
            }));
          }
          body = { items: [{ item_id: "0.1", kind: "agent_chat", payload: "Older", source: null, created_at: null,
            inbound_id: null, show_timestamp: false, source_message_id: "older_0", source_block_idx: 0,
            reading_locator: "locator_older" }], reading_source: "current_source", anchor_item_id: null,
          older_cursor: null, newer_cursor: "locator_0" } satisfies ReadingWindowResponse;
        } else {
          const target = rows.find((item) => item.reading_locator === url.searchParams.get("anchor"));
          if (!target) throw new Error("The fixture does not contain the requested source anchor");
          body = { items: url.searchParams.get("limit") === "1" ? [target] : rows,
            reading_source: "current_source", anchor_item_id: target.item_id,
            older_cursor: olderCursor, newer_cursor: null } satisfies ReadingWindowResponse;
        }
      }
      else if (path.endsWith("/conversation-snapshot")) body = { timeline: timeline(), pending: [], token_usage: {
        input_tokens: 0, output_tokens: 0, reasoning_tokens: 0, max_input_tokens: 0, soft_compact_tokens: 0, hard_compact_tokens: 0,
      } };
      else if (path.endsWith("/timeline")) body = timeline();
      else if (path.endsWith("/user-messages")) body = { mode: "prefix", seen_id: 0, items: [] };
      else if (path.endsWith("/pending") || path === "/api/questions/open") body = [];
      else if (path.endsWith("/to-user")) body = { content: null, version: null, modified_at: null, resources: {}, legacy_html: false };
      else throw new Error(`Unexpected Home reading request ${path}`);
      return Promise.resolve(new Response(JSON.stringify(body), { headers: { "content-type": "application/json" } }));
    },
  });
  const { hub } = fakeStreamHub();
  function Reader({ id }: { id: number }) {
    const live = useTimeline(id, () => undefined, "idling");
    const reading = useReadingWindow(id, `${entry}:agent:${id}`, live);
    useLayoutEffect(() => { state.reading = reading; }, [reading]);
    const props = homePageProps();
    const agents = [makeAgent({ agent_id: 42, status: "idling" }), makeAgent({ agent_id: 43, status: "idling" })];
    return <HomePageView {...props} bfcacheId={entry} timeline={reading} TimelineView={MeasuredTimeline}
      settings={{ settings: { ...USER_SETTING_DEFAULTS, "behavior.replySuggestions": false }, isLoading: false }}
      agents={{ ...props.agents, agents, activeId: id, activeAgent: agents.find((agent) => agent.agent_id === id) }} />;
  }
  const surface = (children: ReactNode) => <QueryClientProvider client={cache}>
    <ApiProvider api={rig.api}><StreamHubProvider hub={hub}><WallClockProvider wallNow={() => 1001}>
      <TestStreamProvider>{children}</TestStreamProvider>
    </WallClockProvider></StreamHubProvider></ApiProvider>
  </QueryClientProvider>;
  const view = renderWithBrowser(surface(<Reader id={42} />), { browser });
  const viewport = () => {
    const node = view.container.querySelector<HTMLElement>('[data-slot="scroll-area-viewport"]');
    if (!node) throw new Error("The Home reading viewport did not mount");
    return node;
  };
  return { state, rows, browser, rig, view, viewport, rerender: (id: number) => view.rerender(surface(<Reader id={id} />)) };
}

beforeEach(async () => { await resetHomePageTestState(); useTimelineStore.getState().switchThread(null, null, false); });
afterEach(async () => { cleanup(); for (const client of clients) client.clear(); clients.length = 0; callbacks.clear(); await cleanupHomePageTest(); });

describe("Home source reading consumer", () => {
  it("restores the exact source item offset and requalifies after A/B/A", async () => {
    const context = setup();
    await waitFor(() => expect(context.state.reading?.sourceReady).toBe(true));
    resize();
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    const offset = () => {
      const target = context.viewport().querySelector<HTMLElement>('.timeline-item[data-item-id="6.0"]');
      if (!target) throw new Error("The canonical Home target did not mount");
      return target.getBoundingClientRect().top - context.viewport().getBoundingClientRect().top;
    };
    expect(offset()).toBe(97);
    context.rerender(43);
    context.rerender(42);
    await waitFor(() => expect(context.state.reading?.sourceReady).toBe(true));
    resize();
    await waitFor(() => expect(screen.queryByTestId("cold-load-spinner")).toBeNull());
    expect(offset()).toBe(97);
    expect(context.rig.sent.filter((request) => request.url.includes("/timeline/window?")).length).toBeGreaterThanOrEqual(2);
  });

  it("keeps the measured visible item offset when the accepted live head trims its prefix", async () => {
    const context = setup();
    await waitFor(() => expect(context.state.reading?.sourceReady).toBe(true));
    resize();
    await waitFor(() => expect(context.state.reading?.isLoading).toBe(false));
    fireEvent.wheel(context.viewport(), { deltaY: 30 });
    act(() => { context.viewport().scrollTop += 30; fireEvent.scroll(context.viewport()); });
    const target = context.viewport().querySelector<HTMLElement>('.timeline-item[data-item-id="6.0"]');
    if (!target) throw new Error("The parked Home reading item did not mount");
    const offset = target.getBoundingClientRect().top - context.viewport().getBoundingClientRect().top;
    context.state.head = context.rows.slice(4);
    const bookmark = readReadingBookmark(context.browser.session, 42, 1001);
    expect(bookmark.state).toBe("ready");
    expect(bookmark.state === "ready" && context.state.head.some((item) => item.reading_locator === bookmark.bookmark?.locator)).toBe(true);
    act(() => context.state.reading?.reading.loadNewer());
    await waitFor(() => expect(context.state.reading?.reading.parked).toBe(false));
    resize();
    const joined = context.viewport().querySelector<HTMLElement>('.timeline-item[data-item-id="6.0"]');
    if (!joined) throw new Error("The accepted live head lost the visible reading member");
    expect(joined.getBoundingClientRect().top - context.viewport().getBoundingClientRect().top).toBe(offset);
    expect(context.browser.sessionValues.get(READING_BOOKMARKS_KEY)).toBeDefined();
  });

  it("floats a refused older-page alert without displacing the reading item or blocking clicks", async () => {
    const context = setup({ olderCursor: "locator_0", failBefore: true });
    await waitFor(() => expect(context.state.reading?.sourceReady).toBe(true));
    resize();
    await waitFor(() => expect(context.state.reading?.isLoading).toBe(false));
    const offset = () => {
      const target = context.viewport().querySelector<HTMLElement>('.timeline-item[data-item-id="6.0"]');
      if (!target) throw new Error("The canonical Home target did not mount");
      return target.getBoundingClientRect().top - context.viewport().getBoundingClientRect().top;
    };
    expect(offset()).toBe(97);
    act(() => context.state.reading?.loadOlder("reader"));
    const alert = await screen.findByRole("alert");
    expect(alert.textContent).toContain("Could not restore your reading position");
    expect(alert.textContent).toContain("Checkpoint history unavailable");
    expect(alert.parentElement?.classList.contains("pointer-events-auto")).toBe(true);
    expect(alert.parentElement?.parentElement?.classList.contains("pointer-events-none")).toBe(true);
    expect(alert.parentElement?.parentElement?.classList.contains("absolute")).toBe(true);
    expect(offset()).toBe(97);
    context.state.failBefore = false;
    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    await waitFor(() => expect(screen.queryByRole("alert")).toBeNull());
    expect(offset()).toBe(97);
  });
});
