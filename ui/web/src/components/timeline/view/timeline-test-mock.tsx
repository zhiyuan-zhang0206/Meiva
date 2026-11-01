import { notifyManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, screen } from "@testing-library/react";

import { renderWithBrowser, type BrowserRenderOptions } from "@/test-support/render";
import type { ReactElement, ReactNode } from "react";
import { vi } from "vitest";

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
    impersonation: overrides.impersonation,
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
