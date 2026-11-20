import { notifyManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";

import { renderWithBrowser, type BrowserRenderOptions } from "@/test-support/render";
import type { ReactElement, ReactNode } from "react";
import { onTestFinished, vi } from "vitest";

import { RIDE_MAX_MS, rideClock } from "../anchor/ride";

import { createLazyChunk } from "@/components/content/lazy-chunk";
import { PythonCodeHighlighterProvider } from "@/components/content/python-code";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem } from "@/lib/contracts/types";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";
import { ApiProvider, WallClockProvider } from "@/lib/transport/transport-context";
import { transport } from "@/test-support/fake-api";

notifyManager.setScheduler((cb) => {
  cb();
});
const pendingHighlighter = createLazyChunk({
  name: "timeline-test-highlighter",
  importModule: () => new Promise<never>(() => undefined),
});

export const timelineQueryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: false,
      staleTime: Infinity,
      gcTime: Infinity,
      refetchOnWindowFocus: false,
    },
  },
});

function realWallNow(): number {
  return Date.now();
}

function TimelineQueryWrapper({ children }: { children: ReactNode }) {
  return (
    <WallClockProvider wallNow={realWallNow}>
      <QueryClientProvider client={timelineQueryClient}>
        <ApiProvider api={transport}>
          <PythonCodeHighlighterProvider value={pendingHighlighter}>
            {children}
          </PythonCodeHighlighterProvider>
        </ApiProvider>
      </QueryClientProvider>
    </WallClockProvider>
  );
}

export function render(ui: ReactElement, options?: BrowserRenderOptions) {
  return renderWithBrowser(ui, { wrapper: TimelineQueryWrapper, ...options });
}

export function historyWheel(viewport: HTMLElement, deltaY: number, momentum: boolean) {
  const event = new WheelEvent("wheel", { deltaY, bubbles: true, cancelable: true });
  Object.defineProperty(event, "momentum", { value: momentum });
  Object.defineProperty(viewport, "onscrollend", { value: null, configurable: true });
  act(() => {
    viewport.dispatchEvent(event);
    if (!event.defaultPrevented) {
      viewport.scrollTop = Math.max(0, viewport.scrollTop + deltaY);
      viewport.dispatchEvent(new Event("scroll"));
    }
  });
  return event;
}

export function pullOlderAtTop(viewport: HTMLElement) {
  historyWheel(viewport, -100, false);
  historyWheel(viewport, -20, true);
}

export const displayLimits = new (class extends Map<string, number> {
  override set(key: string, value: number) {
    super.set(key, value);
    syncDisplayLimitsCache();
    return this;
  }
})();

function syncDisplayLimitsCache() {
  const fields = Array.from(displayLimits.entries()).map(([env_var, current_value]) => ({
    env_var,
    current_value,
  }));
  const view: Record<string, unknown> = {};
  Object.defineProperty(view, "fields", {
    get() {
      settingsState.reads += 1;
      return fields;
    },
    enumerable: true,
    configurable: true,
  });
  act(() => {
    timelineQueryClient.setQueryData(["config", null], view);
  });
}

export function ChatMarkdownMock({ content }: { content: string }) {
  return <div data-testid="chat-markdown">{content}</div>;
}

export function ScrollAreaMock({
  children,
  className,
  viewportClassName,
  viewportRef,
  viewportTestId,
  onScrollCapture,
  ...rest
}: {
  children: React.ReactNode;
  className?: string;
  viewportClassName?: string;
  viewportRef?: React.Ref<HTMLDivElement>;
  viewportTestId?: string;
  onScrollCapture?: React.UIEventHandler<HTMLDivElement>;
  "data-activity-scroll"?: string;
  "data-turn-scroll"?: string;
}) {
  const isActivity = rest["data-activity-scroll"] === "true";
  const isTurnScroll = rest["data-turn-scroll"] === "true";
  return (
    <div
      data-testid={isActivity ? "activity-scroll-area" : isTurnScroll ? "turn-scroll-area" : "scroll-area"}
      data-activity-scroll={rest["data-activity-scroll"]}
      data-turn-scroll={rest["data-turn-scroll"]}
      onScrollCapture={onScrollCapture}
      className={className}
    >
      <div
        ref={viewportRef}
        data-testid={
          viewportTestId ??
          (isActivity ? "activity-scroll-viewport" : isTurnScroll ? "turn-scroll-viewport" : "scroll-viewport")
        }
        data-slot="scroll-area-viewport"
        className={viewportClassName}
      >
        {children}
      </div>
    </div>
  );
}

export const settingsState = {
  reads: 0,
};

export const userSettingsState: Record<string, unknown> = {
  "display.show_timestamp_weekday": true,
  "display.clock_format": "24h",
};

function buildSettingsSnapshot(): Record<string, unknown> {
  const clockFormat = userSettingsState["display.clock_format"];
  const snap: Record<string, unknown> = {
    ...USER_SETTING_DEFAULTS,
    ...userSettingsState,
  };
  Object.defineProperty(snap, "display.clock_format", {
    get() {
      settingsState.reads += 1;
      return clockFormat;
    },
    enumerable: true,
    configurable: true,
  });
  return snap;
}

function syncSettingsCache() {
  // meiva:note
  // The timeline reads settings through the transport; the cache below answers instead, and
  // an unanswered read must not reach the network.
  transport.getSettings.mockImplementation(() => new Promise(() => undefined));
  transport.getConfig.mockImplementation(() => new Promise(() => undefined));
  act(() => {
    timelineQueryClient.setQueryData(SETTINGS_QUERY_KEY, buildSettingsSnapshot());
  });
}

export function setUserSettings(settings: Record<string, unknown>) {
  Object.assign(userSettingsState, settings);
  syncSettingsCache();
}

export function resetUserSettings() {
  for (const key of Object.keys(userSettingsState)) {
    Reflect.deleteProperty(userSettingsState, key);
  }
  userSettingsState["display.show_timestamp_weekday"] = true;
  userSettingsState["display.clock_format"] = "24h";
}

syncSettingsCache();
syncDisplayLimitsCache();

export function cleanupTimelineTest() {
  cleanup();
  resetUserSettings();
  displayLimits.clear();
  syncSettingsCache();
  syncDisplayLimitsCache();
  vi.unstubAllGlobals();
}

export function makeItem(
  overrides: Partial<BackendTimelineItem> & Pick<BackendTimelineItem, "kind" | "payload">,
): BackendTimelineItem {
  return {
    item_id: overrides.item_id ?? "1.0",
    kind: overrides.kind,
    payload: overrides.payload,
    source: overrides.source ?? null,
    created_at: "created_at" in overrides ? (overrides.created_at ?? null) : "2026-05-15T12:00:00Z",
    inbound_id: overrides.inbound_id ?? null,
    show_timestamp: overrides.show_timestamp ?? true,
    user_facing: overrides.user_facing,
    partial: overrides.partial,
    interrupted: overrides.interrupted,
    reasoning_ms: overrides.reasoning_ms,
    reasoning_tokens: overrides.reasoning_tokens,
    reasoningStartedAt: overrides.reasoningStartedAt,
    reasoningElapsedMs: overrides.reasoningElapsedMs,
    codeStartedAt: overrides.codeStartedAt,
    codeElapsedMs: overrides.codeElapsedMs,
    code_elapsed_ms: overrides.code_elapsed_ms,
    execStartedAt: overrides.execStartedAt,
    exec_ms: overrides.exec_ms,
    images: overrides.images ?? null,
    files: overrides.files ?? null,
    image_captions: overrides.image_captions ?? null,
    sdk_calls: overrides.sdk_calls,
  };
}

export function revealCollapsedCards() {
  for (const btn of screen.queryAllByTestId("turn-toggle")) {
    if (btn.getAttribute("data-expanded") === "false" || btn.getAttribute("aria-expanded") === "false") {
      fireEvent.click(btn);
    }
  }
  for (const btn of screen.queryAllByTestId("card-toggle")) {
    if (btn.getAttribute("data-expanded") === "false") fireEvent.click(btn);
  }
}

// meiva:note
// A layout model for the timeline viewport, since the test DOM lays nothing out. Every element
// under the main viewport gets a height and a position derived from the DOM itself:
// - `.timeline-item` rows are `rowHeight` tall (default 80), a turn header 40, spacers and the
//   send reserve their inline height; any other element is as tall as its children;
// - the log has the 52px bar-clear top padding and 24px bottom padding, `space-y-3` / `space-y-1`
//   add their gaps between in-flow children;
// - the column starts at the viewport's top edge.
// scrollTop clamps to the scroll range, the arrow's ride waits for `finishSmooth` (its frames and
// clock are the model's), and every ResizeObserver is delivered on `resize()` with the model's heights.
export interface TimelineLayoutOptions {
  viewportHeight?: number;
  rowHeight?: number | ((row: HTMLElement) => number | undefined);
  // meiva:note
  // Height of any element the model should not derive (return undefined to derive it).
  height?: (element: HTMLElement) => number | undefined;
}

const LOG_PAD_TOP = 52;
const LOG_PAD_BOTTOM = 24;

export function timelineLayout(options: TimelineLayoutOptions = {}) {
  let viewportHeight = options.viewportHeight ?? 600;
  let top = 0;
  const rideFrames = new Map<number, () => void>();
  let rideFrameId = 0;
  let rideNow = 0;
  const realRideClock = { ...rideClock };
  rideClock.frame = (callback) => { rideFrameId += 1; rideFrames.set(rideFrameId, callback); return rideFrameId; };
  rideClock.cancel = (id) => { rideFrames.delete(id); };
  rideClock.now = () => rideNow;
  onTestFinished(() => { Object.assign(rideClock, realRideClock); });
  const writes: number[] = [];
  const observers: { callback: ResizeObserverCallback; targets: Set<Element>; instance: ResizeObserver }[] = [];

  const isViewport = (el: Element) => el.getAttribute("data-testid") === "scroll-viewport";
  const viewportOf = (el: HTMLElement) => el.closest<HTMLElement>('[data-testid="scroll-viewport"]');
  const inFlow = (el: Element): el is HTMLElement =>
    el instanceof HTMLElement && !el.classList.contains("sr-only") && !el.hidden && el.style.display !== "none";
  const gapOf = (el: HTMLElement) => /\bspace-y-3\b/.test(el.className) ? 12 : /\bspace-y-1\b/.test(el.className) ? 4 : 0;
  const isLog = (el: HTMLElement) => el.getAttribute("role") === "log";

  const leafHeight = (el: HTMLElement): number | undefined => {
    const custom = options.height?.(el);
    if (custom !== undefined) return custom;
    if (el.hasAttribute("data-timeline-reserve") || el.hasAttribute("data-timeline-spacer")) {
      return Number.parseFloat(el.style.height) || 0;
    }
    if (el.classList.contains("timeline-item")) {
      const row = typeof options.rowHeight === "function" ? options.rowHeight(el) : options.rowHeight;
      return row ?? 80;
    }
    if (el.dataset.testid === "turn-toggle") return 40;
    return undefined;
  };

  const heightOf = (el: HTMLElement, memo: Map<Element, number>): number => {
    const known = memo.get(el);
    if (known !== undefined) return known;
    let height = leafHeight(el);
    if (height === undefined) {
      const gap = gapOf(el);
      let sum = 0;
      let count = 0;
      for (const child of el.children) {
        if (!inFlow(child)) continue;
        sum += heightOf(child, memo) + (count > 0 ? gap : 0);
        count += 1;
      }
      height = sum + (isLog(el) ? LOG_PAD_TOP + LOG_PAD_BOTTOM : 0);
    }
    memo.set(el, height);
    return height;
  };

  const columnHeight = (vp: HTMLElement, memo: Map<Element, number>) => {
    let sum = 0;
    for (const child of vp.children) if (inFlow(child)) sum += heightOf(child, memo);
    return sum;
  };

  // meiva:note
  // Position of an element in the viewport's scroll space.
  const docTop = (el: HTMLElement, vp: HTMLElement, memo: Map<Element, number>): number => {
    if (el === vp) return 0;
    const parent = el.parentElement;
    if (!parent) return 0;
    if (parent === vp) {
      let y = 0;
      for (const sibling of vp.children) {
        if (sibling === el) break;
        if (inFlow(sibling)) y += heightOf(sibling, memo);
      }
      return y;
    }
    let y = docTop(parent, vp, memo) + (isLog(parent) ? LOG_PAD_TOP : 0);
    const gap = gapOf(parent);
    for (const sibling of parent.children) {
      if (sibling === el) break;
      if (!inFlow(sibling)) continue;
      y += heightOf(sibling, memo) + gap;
    }
    return y;
  };

  const scrollHeightOf = (vp: HTMLElement) => Math.max(viewportHeight, columnHeight(vp, new Map()));
  const maxTop = (vp: HTMLElement) => Math.max(0, scrollHeightOf(vp) - viewportHeight);
  const clamp = (vp: HTMLElement, value: number) => Math.min(Math.max(0, value), maxTop(vp));

  const descriptor = (key: string) => {
    for (let proto: object | null = HTMLElement.prototype; proto; proto = Object.getPrototypeOf(proto) as object | null) {
      const found = Object.getOwnPropertyDescriptor(proto, key);
      if (found) return found;
    }
    throw new Error(`no ${key}`);
  };
  const getter = (key: string): ((this: Element) => number) => {
    const found = descriptor(key);
    return function (this: Element) { return Reflect.apply(Reflect.get(found, "get") as (this: Element) => number, this, []); };
  };
  const setter = (key: string): ((this: Element, value: number) => void) => {
    const found = descriptor(key);
    return function (this: Element, value: number) { Reflect.apply(Reflect.get(found, "set") as (this: Element, value: number) => void, this, [value]); };
  };
  const original = {
    scrollTop: { get: getter("scrollTop"), set: setter("scrollTop") },
    scrollHeight: { get: getter("scrollHeight") },
    clientHeight: { get: getter("clientHeight") },
    offsetHeight: { get: getter("offsetHeight") },
    rect: descriptor("getBoundingClientRect").value as (this: Element) => DOMRect,
    scrollTo: descriptor("scrollTo").value as (this: Element, ...args: unknown[]) => void,
  };
  const box = (y: number, height: number): DOMRect =>
    ({ top: y, bottom: y + height, height, left: 0, right: 800, width: 800, x: 0, y, toJSON: () => ({}) });

  // meiva:note
  // Installed as own properties of HTMLElement.prototype and removed when the test finishes: a
  // spied inherited getter survives `vi.restoreAllMocks()` and would leak the model into later tests.
  const shadowed = new Map<string, PropertyDescriptor | undefined>();
  const install = (key: string, value: PropertyDescriptor) => {
    if (!shadowed.has(key)) shadowed.set(key, Object.getOwnPropertyDescriptor(HTMLElement.prototype, key));
    Object.defineProperty(HTMLElement.prototype, key, { configurable: true, ...value });
  };
  onTestFinished(() => {
    for (const [key, previous] of shadowed) {
      if (previous) Object.defineProperty(HTMLElement.prototype, key, previous);
      else Reflect.deleteProperty(HTMLElement.prototype, key);
    }
  });
  install("scrollTop", {
    get(this: HTMLElement) {
      if (!isViewport(this)) return original.scrollTop.get.call(this);
      // meiva:note
      // A browser clamps the position as soon as the scroll range shrinks under it.
      top = clamp(this, top);
      return top;
    },
    set(this: HTMLElement, value: number) {
      if (!isViewport(this)) {
        original.scrollTop.set.call(this, value);
        return;
      }
      writes.push(value);
      top = clamp(this, value);
    },
  });
  install("scrollHeight", {
    get(this: HTMLElement) {
      return isViewport(this) ? scrollHeightOf(this) : original.scrollHeight.get.call(this);
    },
  });
  install("clientHeight", {
    get(this: HTMLElement) {
      if (isViewport(this)) return viewportHeight;
      return viewportOf(this) ? heightOf(this, new Map()) : original.clientHeight.get.call(this);
    },
  });
  install("offsetHeight", {
    get(this: HTMLElement) {
      if (isViewport(this)) return viewportHeight;
      return viewportOf(this) ? heightOf(this, new Map()) : original.offsetHeight.get.call(this);
    },
  });
  install("getBoundingClientRect", {
    writable: true,
    value(this: HTMLElement) {
      if (isViewport(this)) return box(0, viewportHeight);
      const vp = viewportOf(this);
      if (!vp) return original.rect.call(this);
      top = clamp(vp, top);
      const memo = new Map<Element, number>();
      return box(docTop(this, vp, memo) - top, heightOf(this, memo));
    },
  });
  install("scrollTo", {
    writable: true,
    value(this: HTMLElement, ...args: unknown[]) {
      if (!isViewport(this)) {
        original.scrollTo.call(this, ...args);
        return;
      }
      const [first] = args;
      const target = typeof first === "object" && first !== null ? (first as ScrollToOptions).top : (args[1] as number | undefined);
      if (target === undefined) return;
      top = clamp(this, target);
    },
  });

  vi.stubGlobal("ResizeObserver", class {
    private readonly record: (typeof observers)[number];
    constructor(callback: ResizeObserverCallback) {
      this.record = { callback, targets: new Set(), instance: this };
      observers.push(this.record);
    }
    observe(target: Element) { this.record.targets.add(target); }
    unobserve(target: Element) { this.record.targets.delete(target); }
    disconnect() { this.record.targets.clear(); }
    takeRecords() { return []; }
  });

  const viewport = () => screen.getByTestId("scroll-viewport");
  const fire = (vp: HTMLElement) => vp.dispatchEvent(new Event("scroll"));
  const rowNode = (itemId: string) => viewport().querySelector<HTMLElement>(
    `.timeline-item[data-item-id="${CSS.escape(itemId)}"], [data-turn-id][data-item-id="${CSS.escape(itemId)}"]`,
  );

  const naturalEnd = () => {
    const vp = viewport();
    const content = vp.querySelector<HTMLElement>('[role="log"]');
    const memo = new Map<Element, number>();
    return content ? Math.max(0, docTop(content, vp, memo) + heightOf(content, memo) - viewportHeight) : 0;
  };

  return {
    viewport,
    // meiva:note
    // Every scrollTop write since the last `writes.length = 0`.
    writes,
    top: () => viewport().scrollTop,
    max: () => maxTop(viewport()),
    scrollHeight: () => scrollHeightOf(viewport()),
    reserve: () => Number.parseFloat(viewport().querySelector<HTMLElement>("[data-timeline-reserve]")?.style.height ?? "") || 0,
    // meiva:note
    // The scroll offset at which the content's last row meets the viewport bottom.
    naturalEnd,
    // meiva:note
    // A row's top edge relative to the viewport top.
    rowTop: (itemId: string) => {
      const node = rowNode(itemId);
      if (!node) throw new Error(`row ${itemId} is not mounted`);
      return node.getBoundingClientRect().top;
    },
    rowNode,
    setViewportHeight: (height: number) => { viewportHeight = height; },
    // meiva:note
    // The reader scrolls to a position (the browser's scroll event follows).
    scrollTo: (next: number) => {
      const vp = viewport();
      act(() => {
        top = clamp(vp, next);
        fire(vp);
      });
    },
    // meiva:note
    // The position moves without a scroll event yet (a smooth wheel mid-animation).
    drift: (next: number) => { top = clamp(viewport(), next); },
    // meiva:note
    // A scroll event at the current position (the browser echoing a programmatic write).
    echo: () => { act(() => { fire(viewport()); }); },
    wheel: (deltaY: number) => {
      const vp = viewport();
      const event = new WheelEvent("wheel", { deltaY, bubbles: true, cancelable: true });
      act(() => {
        vp.dispatchEvent(event);
        if (!event.defaultPrevented) {
          top = clamp(vp, top + deltaY);
          fire(vp);
        }
      });
      return event;
    },
    touchStart: () => { act(() => { fireEvent.touchStart(viewport(), { touches: [{ clientY: 300 }] }); }); },
    touchEnd: () => { act(() => { fireEvent.touchEnd(viewport(), { touches: [] }); }); },
    // meiva:note
    // Deliver every ResizeObserver with the model's current heights.
    resize: () => {
      act(() => {
        for (const { callback, targets, instance } of [...observers]) {
          const live = [...targets].filter((target) => target.isConnected);
          if (live.length === 0) continue;
          callback(live.map((target) => {
            const height = target instanceof HTMLElement ? target.offsetHeight : 0;
            return { target, borderBoxSize: [{ blockSize: height, inlineSize: 800 }], contentRect: box(0, height) } as unknown as ResizeObserverEntry;
          }), instance);
        }
      });
    },
    // meiva:note
    // Where a running ride is heading (the live natural end), or null when no ride runs.
    smoothTarget: () => (rideFrames.size > 0 ? naturalEnd() : null),
    // meiva:note
    // One frame of the ride, `ms` after the previous one.
    rideStep: (ms: number) => {
      act(() => {
        rideNow += ms;
        const pending = [...rideFrames];
        rideFrames.clear();
        for (const [, callback] of pending) callback();
      });
    },
    // meiva:note
    // The ride's deadline passes: its last frame lands on the end, then the browser echoes the write.
    finishSmooth: () => {
      const vp = viewport();
      act(() => {
        rideNow += RIDE_MAX_MS;
        while (rideFrames.size > 0) {
          const pending = [...rideFrames];
          rideFrames.clear();
          for (const [, callback] of pending) callback();
        }
        fire(vp);
      });
    },
  };
}
