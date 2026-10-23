import { act, cleanup, fireEvent, screen } from "@testing-library/react";
import { vi } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";

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

let settingsVersion = 0;
const settingsListeners = new Set<() => void>();

export const mockSettings = {
  version: () => settingsVersion,
  subscribe: (listener: () => void) => {
    settingsListeners.add(listener);
    return () => {
      settingsListeners.delete(listener);
    };
  },
  notify: () => {
    settingsVersion += 1;
    for (const listener of settingsListeners) listener();
  },
  onChange: () => {
    act(() => {
      mockSettings.notify();
    });
  },
};

export const displayLimits = new (class extends Map<string, number> {
  override set(key: string, value: number) {
    super.set(key, value);
    mockSettings.onChange();
    return this;
  }
})();

export function ChatMarkdownMock({ content }: { content: string }) {
  return <div data-testid="chat-markdown">{content}</div>;
}

export function PythonCodeMock({ code, streaming }: { code: string; streaming?: boolean }) {
  return (
    <div className="group relative">
      <button type="button" aria-label="Copy code">
        Copy
      </button>
      <pre data-testid="python-code" data-streaming={streaming ? "1" : "0"}>
        {code}
      </pre>
    </div>
  );
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
  resetPending: false,
  reads: 0,
};

export function setToggleState(patch: Partial<typeof toggleState>) {
  Object.assign(toggleState, patch);
  mockSettings.onChange();
}

export function resetToggleState() {
  toggleState.detailsMode = "all";
  toggleState.isLoading = false;
  toggleState.resetPending = false;
}

function setResetPending(): void {
  toggleState.resetPending = true;
}

function clearResetPending(): void {
  toggleState.resetPending = false;
}

export const resetStore = {
  get resetPending() {
    return toggleState.resetPending;
  },
  bumpReset: vi.fn(setResetPending),
  consumeReset: vi.fn(clearResetPending),
};

export const userSettingsState: Record<string, unknown> = {
  "display.render_reasoning_markdown": true,
  "display.show_timestamp_weekday": true,
  "display.clock_format": "24h",
  "display.show_timezone": false,
};

export function setUserSettings(settings: Record<string, unknown>) {
  Object.assign(userSettingsState, settings);
}

export function resetUserSettings() {
  userSettingsState["display.render_reasoning_markdown"] = true;
  userSettingsState["display.show_timestamp_weekday"] = true;
  userSettingsState["display.clock_format"] = "24h";
  userSettingsState["display.show_timezone"] = false;
}

export function cleanupTimelineTest() {
  cleanup();
  resetToggleState();
  resetUserSettings();
  displayLimits.clear();
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
