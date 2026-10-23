import { notifyManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render as rtlRender, type RenderOptions, screen } from "@testing-library/react";
import type { ReactElement, ReactNode } from "react";
import { vi } from "vitest";

import {
  __resetHighlighterCacheForTests,
  __setHighlighterImportForTests,
} from "@/components/content/python-code";
import { USER_SETTING_DEFAULTS, type BackendTimelineItem } from "@/lib/contracts/types";
import { useContentToggleReset } from "@/lib/layout/content-toggle-store";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";
import { api } from "@/lib/transport/api";

notifyManager.setScheduler((cb) => {
  cb();
});
__resetHighlighterCacheForTests();
__setHighlighterImportForTests(() => new Promise(() => undefined));

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

function TimelineQueryWrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={timelineQueryClient}>{children}</QueryClientProvider>;
}

export function render(ui: ReactElement, options?: RenderOptions) {
  return rtlRender(ui, { wrapper: TimelineQueryWrapper, ...options });
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
  act(() => {
    timelineQueryClient.setQueryData(["config", null], { fields });
  });
}

export function ChatMarkdownMock({ content }: { content: string }) {
  return <div data-testid="chat-markdown">{content}</div>;
}

export function ScrollAreaMock({
  children,
  className,
  viewportClassName,
}: {
  children: React.ReactNode;
  className?: string;
  viewportClassName?: string;
}) {
  return (
    <div data-testid="scroll-area" className={className}>
      <div
        data-testid="scroll-viewport"
        data-slot="scroll-area-viewport"
        className={viewportClassName}
      >
        {children}
      </div>
    </div>
  );
}

export const toggleState = {
  detailsMode: "all" as "all" | "last" | "none",
  isLoading: false,
  reads: 0,
};

export const userSettingsState: Record<string, unknown> = {
  "display.render_reasoning_markdown": true,
  "display.show_timestamp_weekday": true,
  "display.clock_format": "24h",
  "display.show_timezone": false,
};

function buildSettingsSnapshot(): Record<string, unknown> {
  const mode = toggleState.detailsMode;
  const snap: Record<string, unknown> = {
    ...USER_SETTING_DEFAULTS,
    ...userSettingsState,
  };
  Object.defineProperty(snap, "display.expand_runs_mode", {
    get() {
      toggleState.reads += 1;
      return mode;
    },
    enumerable: true,
    configurable: true,
  });
  return snap;
}

function syncSettingsCache() {
  vi.spyOn(api, "getSettings").mockImplementation(() => new Promise(() => undefined));
  vi.spyOn(api, "getConfig").mockImplementation(() => new Promise(() => undefined));
  act(() => {
    if (toggleState.isLoading) {
      timelineQueryClient.removeQueries({ queryKey: SETTINGS_QUERY_KEY });
    } else {
      timelineQueryClient.setQueryData(SETTINGS_QUERY_KEY, buildSettingsSnapshot());
    }
  });
}

export function setToggleState(patch: Partial<typeof toggleState>) {
  Object.assign(toggleState, patch);
  syncSettingsCache();
}

export function resetToggleState() {
  toggleState.detailsMode = "all";
  toggleState.isLoading = false;
  useContentToggleReset.setState({ resetPending: false });
}

export function setUserSettings(settings: Record<string, unknown>) {
  Object.assign(userSettingsState, settings);
  syncSettingsCache();
}

export function resetUserSettings() {
  for (const key of Object.keys(userSettingsState)) {
    Reflect.deleteProperty(userSettingsState, key);
  }
  userSettingsState["display.render_reasoning_markdown"] = true;
  userSettingsState["display.show_timestamp_weekday"] = true;
  userSettingsState["display.clock_format"] = "24h";
  userSettingsState["display.show_timezone"] = false;
}

syncSettingsCache();
syncDisplayLimitsCache();

export function cleanupTimelineTest() {
  cleanup();
  resetToggleState();
  resetUserSettings();
  displayLimits.clear();
  syncSettingsCache();
  syncDisplayLimitsCache();
  __resetHighlighterCacheForTests();
  __setHighlighterImportForTests(() => new Promise(() => undefined));
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
    image_captions: overrides.image_captions ?? null,
    sdk_calls: overrides.sdk_calls,
  };
}

export function revealCollapsedCards() {
  for (const btn of screen.queryAllByTestId("card-toggle")) {
    if (btn.getAttribute("data-expanded") === "false") fireEvent.click(btn);
  }
}
