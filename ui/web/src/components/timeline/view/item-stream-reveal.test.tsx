// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md

import { act, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { BackendTimelineItem } from "@/lib/contracts/types";

vi.mock("@/components/content/markdown", () => ({
  ChatMarkdown: ({ content }: { content: string }) => <div data-testid="md">{content}</div>,
}));
vi.mock("@/components/content/python-code", () => ({
  PythonCode: ({ code }: { code: string }) => <pre data-testid="code">{code}</pre>,
}));

import type { HighlightHost, HighlightLike } from "@/lib/timeline/reveal/edge-fade";
import { REVEAL_SETTLE_MS } from "@/lib/timeline/reveal/constants";
import { EdgeFadeHostContext } from "@/lib/timeline/reveal/use-stream-edge-fade";
import { renderWithBrowser } from "@/test-support/render";
import type { ReactElement, ReactNode } from "react";

import { ItemView } from "../item";

function chat(payload: string, extra: Partial<BackendTimelineItem> = {}): BackendTimelineItem {
  return {
    item_id: "7.1",
    kind: "agent_chat",
    source: null,
    payload,
    created_at: "2026-11-25T00:00:00Z",
    inbound_id: null,
    show_timestamp: true,
    partial: true,
    ...extra,
  };
}

class FakeHost implements HighlightHost {
  readonly ranges = new Set<Range>();
  highlight(): HighlightLike {
    return this.ranges;
  }
  adoptStyles(): void {
    return undefined;
  }
}

function renderFading(ui: ReactElement, host: HighlightHost | null) {
  return renderWithBrowser(ui, {
    wrapper: ({ children }: { children: ReactNode }) => (
      <EdgeFadeHostContext.Provider value={host}>{children}</EdgeFadeHostContext.Provider>
    ),
  });
}

const render = renderWithBrowser;

const shown = () => screen.getByTestId("md").textContent;

async function frames(ms: number) {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(ms);
  });
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["setTimeout", "clearTimeout", "requestAnimationFrame", "cancelAnimationFrame", "performance"] });
});
afterEach(() => {
  vi.useRealTimers();
  Object.defineProperty(document, "hidden", { configurable: true, value: false });
});

describe("ItemView streaming reveal", () => {
  it("shows text that is already there at once, and paces only what arrives afterwards", async () => {
    const { rerender } = render(<ItemView item={chat("history that was already streamed")} streaming />);
    expect(shown()).toBe("history that was already streamed");
    const full = "history that was already streamed and then a long new burst of text arrives in one packet";
    rerender(<ItemView item={chat(full)} streaming />);
    await frames(50);
    expect(shown().length).toBeGreaterThan("history that was already streamed".length);
    expect(shown().length).toBeLessThan(full.length);
    expect(full.startsWith(shown())).toBe(true);
    await frames(1500);
    expect(shown()).toBe(full);
  });

  it("never displays ahead of arrival and grows monotonically", async () => {
    const { rerender } = render(<ItemView item={chat("")} streaming />);
    let arrived = "";
    let last = 0;
    for (let i = 0; i < 20; i++) {
      arrived += "word ".repeat(6);
      rerender(<ItemView item={chat(arrived)} streaming />);
      await frames(60);
      expect(arrived.startsWith(shown())).toBe(true);
      expect(shown().length).toBeGreaterThanOrEqual(last);
      last = shown().length;
    }
    expect(last).toBeGreaterThan(0);
  });

  it("snapshot commit continues the reveal seamlessly: no reset, no shrink, flushed within the settle window", async () => {
    const live = "The quick brown fox jumps over the lazy dog and keeps running far away. ";
    const { rerender } = render(<ItemView item={chat("")} streaming />);
    rerender(<ItemView item={chat(live)} streaming />);
    await frames(120);
    const before = shown();
    expect(before.length).toBeGreaterThan(0);
    expect(before.length).toBeLessThan(live.length);
    const committed = `${live}The final sentence arrives with the commit.`;
    rerender(<ItemView item={chat(committed, { partial: false })} streaming={false} />);
    expect(shown().startsWith(before)).toBe(true);
    expect(shown().length).toBeGreaterThanOrEqual(before.length);
    await frames(REVEAL_SETTLE_MS + 100);
    expect(shown()).toBe(committed);
  });

  it("a completed item (history, refresh, paging) renders in full without any animation", async () => {
    render(<ItemView item={chat("a finished answer", { partial: false })} streaming={false} />);
    expect(shown()).toBe("a finished answer");
    await frames(10);
    expect(shown()).toBe("a finished answer");
  });

  it("a hidden tab catches up the moment it becomes visible again", async () => {
    const { rerender } = render(<ItemView item={chat("")} streaming />);
    rerender(<ItemView item={chat("z".repeat(2000))} streaming />);
    await frames(30);
    expect(shown().length).toBeLessThan(2000);
    Object.defineProperty(document, "hidden", { configurable: true, value: true });
    document.dispatchEvent(new Event("visibilitychange"));
    Object.defineProperty(document, "hidden", { configurable: true, value: false });
    act(() => {
      document.dispatchEvent(new Event("visibilitychange"));
    });
    expect(shown()).toBe("z".repeat(2000));
  });

  it("closes a half-revealed inline construct for the renderer only while revealing", async () => {
    const { rerender } = render(<ItemView item={chat("")} streaming />);
    rerender(<ItemView item={chat("this is **bold and")} streaming />);
    await frames(2000);
    expect(shown()).toBe("this is **bold and**");
    rerender(<ItemView item={chat("this is **bold and done**", { partial: false })} streaming={false} />);
    await frames(REVEAL_SETTLE_MS + 100);
    expect(shown()).toBe("this is **bold and done**");
  });

  it("an interrupted partial chat stops being live: full text and no repair", () => {
    render(<ItemView item={chat("cut off **mid", { interrupted: true })} streaming={false} />);
    expect(shown()).toBe("cut off **mid");
  });

  it("reveals streaming code without cutting a grapheme", async () => {
    const item = (code: string): BackendTimelineItem => ({
      ...chat(code),
      item_id: "7.2",
      kind: "agent_code",
      partial: undefined,
    });
    const { rerender } = render(<ItemView item={item("")} streaming />);
    const code = "print('👩🏽‍💻 hello')\n".repeat(20);
    rerender(<ItemView item={item(code)} streaming />);
    await frames(300);
    const text = screen.getByTestId("code").textContent;
    expect(text.length).toBeGreaterThan(0);
    expect(code.startsWith(text)).toBe(true);
    await frames(3000);
    expect(screen.getByTestId("code").textContent).toBe(code);
  });

  describe("leading-edge fade never outlives the reveal", () => {
    const sentence = "so the open question is where that structure enters.";

    async function settleFrames() {
      for (let i = 0; i < 15; i++) await frames(100);
    }

    it("a stream that pauses mid-sentence (still partial) ages the edge out", async () => {
      const host = new FakeHost();
      const { rerender } = renderFading(<ItemView item={chat("")} streaming />, host);
      rerender(<ItemView item={chat(sentence)} streaming />);
      await frames(100);
      expect(host.ranges.size).toBeGreaterThan(0);
      await settleFrames();
      expect(shown()).toBe(sentence);
      expect(host.ranges.size).toBe(0);
    });

    it("a new delta after the pause paints the edge again, and it ages out again", async () => {
      const host = new FakeHost();
      const { rerender } = renderFading(<ItemView item={chat("")} streaming />, host);
      rerender(<ItemView item={chat(sentence)} streaming />);
      await settleFrames();
      expect(host.ranges.size).toBe(0);
      rerender(<ItemView item={chat(`${sentence} And then the stream resumes with more words.`)} streaming />);
      await frames(100);
      expect(host.ranges.size).toBeGreaterThan(0);
      await settleFrames();
      expect(host.ranges.size).toBe(0);
    });

    it("an interrupted stream (never committed) leaves nothing translucent", async () => {
      const host = new FakeHost();
      const { rerender } = renderFading(<ItemView item={chat("")} streaming />, host);
      rerender(<ItemView item={chat(sentence)} streaming />);
      await frames(100);
      rerender(<ItemView item={chat(sentence, { interrupted: true })} streaming={false} />);
      await settleFrames();
      expect(host.ranges.size).toBe(0);
    });

    it("a hidden tab that becomes visible shows everything with no translucent edge left", async () => {
      const host = new FakeHost();
      const { rerender } = renderFading(<ItemView item={chat("")} streaming />, host);
      rerender(<ItemView item={chat("z".repeat(600))} streaming />);
      await frames(30);
      Object.defineProperty(document, "hidden", { configurable: true, value: true });
      document.dispatchEvent(new Event("visibilitychange"));
      Object.defineProperty(document, "hidden", { configurable: true, value: false });
      act(() => {
        document.dispatchEvent(new Event("visibilitychange"));
      });
      await settleFrames();
      expect(shown()).toBe("z".repeat(600));
      expect(host.ranges.size).toBe(0);
    });

    it("a committed row that takes over leaves no ranges", async () => {
      const host = new FakeHost();
      const { rerender } = renderFading(<ItemView item={chat("")} streaming />, host);
      rerender(<ItemView item={chat(sentence)} streaming />);
      await frames(100);
      rerender(<ItemView item={chat(sentence, { partial: false })} streaming={false} />);
      await settleFrames();
      expect(host.ranges.size).toBe(0);
    });

    it("never paints a row that mounts complete", async () => {
      const host = new FakeHost();
      renderFading(<ItemView item={chat(sentence)} streaming />, host);
      await frames(100);
      expect(host.ranges.size).toBe(0);
    });
  });
});
