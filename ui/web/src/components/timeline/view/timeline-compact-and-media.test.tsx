import { act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { ChatMarkdownMock, ScrollAreaMock, cleanupTimelineTest, historyWheel, makeItem, pullOlderAtTop, render } from "./timeline-test-mock";
vi.mock("@/components/content/markdown", () => ({ ChatMarkdown: ChatMarkdownMock }));
vi.mock("@/components/ui/scroll-area", () => ({ ScrollArea: ScrollAreaMock }));
import { TimelineView } from "..";
afterEach(cleanupTimelineTest);
describe("continuous compact history", () => {
  it("preserves buffered history and masks canonical duplicates", () => {
    const oldSummary = makeItem({ item_id: "s1.previous.1.0", kind: "inbound_compact_summary", payload: "old summary" });
    const oldest = makeItem({ item_id: "s1.previous.2.0", kind: "agent_chat", payload: "oldest reply" });
    const newSummary = makeItem({ item_id: "1.0", kind: "inbound_compact_summary", payload: "recent summary" });
    const recent = makeItem({ item_id: "2.0", kind: "agent_chat", payload: "recent reply" });
    const compactBuffer: CompactTransitionBuffer = {
      threadId: 42,
      epoch: 3,
      rows: [
        { item: oldSummary, rank: 2, needsCanonicalRow: false },
        { item: oldest, rank: 2, needsCanonicalRow: true },
        { item: newSummary, rank: 1, needsCanonicalRow: false },
        { item: recent, rank: 1, needsCanonicalRow: true },
      ],
      oldestRealByRank: new Map([[2, oldest.item_id], [1, recent.item_id]]),
      tailReady: false,
    };
    const { container } = render(<TimelineView
      items={[
        makeItem({ item_id: "s2.previous.2.0", kind: "agent_chat", payload: "oldest reply" }),
        makeItem({ item_id: "s1.current.2.0", kind: "agent_chat", payload: "recent reply" }),
        makeItem({ item_id: "1.0", kind: "agent_chat", payload: "new current" }),
      ]}
      compactBuffer={compactBuffer}
    />);
    expect(screen.queryByTestId("compact-history-divider")).toBeNull();
    expect(screen.getAllByText("oldest reply")).toHaveLength(1);
    expect(screen.getAllByText("recent reply")).toHaveLength(1);
    expect(container.querySelectorAll('[data-timeline-source="buffer"][data-display-rank="1"]')).not.toHaveLength(0);
    expect(container.querySelectorAll('[data-timeline-source="canonical"][data-item-id="1.0"]')).not.toHaveLength(0);
  });
  it("renders each segment in order without compact separators", () => {
    const items = [
      makeItem({
        item_id: "s2.older-boundary.0.0",
        kind: "system_marker",
        source: "memory",
        payload: "standing context",
      }),
      makeItem({
        item_id: "s2.older-boundary.1.0",
        kind: "inbound_compact_summary",
        payload: "older summary",
      }),
      makeItem({ item_id: "s2.older-boundary.2.0", kind: "agent_chat", payload: "older" }),
      makeItem({
        item_id: "s1.newer-boundary.0.0",
        kind: "inbound_compact_summary",
        payload: "newer summary",
      }),
      makeItem({ item_id: "s1.newer-boundary.1.0", kind: "agent_chat", payload: "newer" }),
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "current" }),
    ];
    const { container } = render(<TimelineView items={items} />);
    expect(screen.queryByTestId("compact-history-divider")).toBeNull();
    expect(screen.queryByText("Original history before compact")).toBeNull();
    expect(screen.queryByText("Context compacted")).toBeNull();
    const timelineColumn = container.querySelector('[role="log"]');
    expect(timelineColumn).not.toBeNull();
    const renderedOrder = Array.from(timelineColumn?.querySelectorAll(":scope > [data-virtual-group] > *") ?? [])
      .filter(
        (node) =>
          node.hasAttribute("data-item-id") ||
          node.getAttribute("data-testid") === "compact-history-divider",
      )
      .map((node) =>
        node.getAttribute("data-testid") === "compact-history-divider"
          ? `divider:${node.getAttribute("data-segment-rank")}`
          : node.getAttribute("data-item-id"),
      );
    expect(renderedOrder).toEqual([
      "s2.older-boundary.0.0",
      "s2.older-boundary.2.0",
      "s1.newer-boundary.0.0",
      "s1.newer-boundary.1.0",
      "1.0",
    ]);
    const mergedFirstTurn = container.querySelector('[data-item-id="s2.older-boundary.0.0"]');
    expect(mergedFirstTurn?.getAttribute("data-turn-member-ids")).toBe(
      "s2.older-boundary.0.0 s2.older-boundary.1.0",
    );
  });
  it("merges adjacent secondary items across compact summaries, historical segments, and silent markers into one single TurnBlock", () => {
    const items = [
      makeItem({ item_id: "s1.prev.0.0", kind: "system_marker", source: "memory", payload: "mem 1" }),
      makeItem({ item_id: "s1.prev.0.1", kind: "system_marker", source: "timezone", payload: "tz" }),
      makeItem({ item_id: "s1.prev.1.0", kind: "inbound_compact_summary", payload: "summary 1" }),
      makeItem({ item_id: "s1.prev.2.0", kind: "agent_reasoning", payload: "t1", reasoning_ms: 4_000 }),
      makeItem({ item_id: "s1.prev.2.1", kind: "agent_code", payload: "x = 1", code_elapsed_ms: 2_000 }),
      makeItem({ item_id: "s1.prev.2.2", kind: "code_output", payload: "ok", exec_ms: 1_000 }),
      makeItem({ item_id: "_marker.compact_done", kind: "system_marker", source: null, payload: "compact_done" }),
      makeItem({ item_id: "0.0", kind: "system_prompt", payload: "sys" }),
      makeItem({ item_id: "1.0", kind: "inbound_compact_summary", payload: "summary 2" }),
      makeItem({ item_id: "2.0", kind: "system_marker", source: "memory", payload: "mem 2" }),
      makeItem({ item_id: "3.0", kind: "agent_reasoning", payload: "t2", reasoning_ms: 3_000 }),
      makeItem({ item_id: "3.1", kind: "agent_chat", payload: "internal note", user_facing: false }),
    ];
    const { container } = render(<TimelineView items={items} />);
    const turns = container.querySelectorAll("[data-turn-id]");
    expect(turns).toHaveLength(1);
    expect(turns[0].getAttribute("data-turn-member-ranks")).toContain("1:s1.prev.0.0");
    expect(turns[0].getAttribute("data-turn-member-ranks")).toContain("0:3.1");
    expect(turns[0].textContent).toContain("Worked for 10s");
    expect(turns[0].textContent).toContain("2 compact summaries");
  });
  it("keeps the oldest segment visible when it has no summary", () => {
    const items = [
      makeItem({ item_id: "s1.old-boundary.0.0", kind: "agent_chat", payload: "old" }),
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "current" }),
    ];
    const { container } = render(<TimelineView items={items} />);
    const timelineColumn = container.querySelector('[role="log"]');
    const renderedOrder = Array.from(timelineColumn?.querySelectorAll(":scope > [data-virtual-group] > *") ?? [])
      .filter(
        (node) =>
          node.hasAttribute("data-item-id") ||
          node.getAttribute("data-testid") === "compact-history-divider",
      )
      .map((node) =>
        node.getAttribute("data-testid") === "compact-history-divider"
          ? "divider"
          : node.getAttribute("data-item-id"),
      );
    expect(renderedOrder).toEqual(["s1.old-boundary.0.0", "1.0"]);
    expect(screen.queryByTestId("compact-history-divider")).toBeNull();
  });
  it("adds no compact separator and reveals the keyboard paging control only on focus", () => {
    const items = [
      makeItem({ item_id: "s1.old-boundary.0.0", kind: "agent_chat", payload: "old" }),
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "current" }),
    ];
    render(<TimelineView items={items} hasMoreOlder onLoadOlder={vi.fn()} />);
    expect(screen.queryByTestId("load-older-divider")).toBeNull();
    expect(screen.queryByTestId("load-older-button")).toBeNull();
    expect(screen.queryByTestId("compact-history-divider")).toBeNull();
    expect(screen.getByTestId("load-older-keyboard").className).toContain("sr-only focus:not-sr-only");
  });
  it("lets a keyboard user request older messages without a wheel gesture", () => {
    const loadOlder = vi.fn();
    render(<TimelineView items={[makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })]} hasMoreOlder onLoadOlder={loadOlder} />);
    fireEvent.click(screen.getByRole("button", { name: "Load earlier messages" }));
    expect(loadOlder).toHaveBeenCalledOnce();
  });
  it("top arrival does not fetch; a separate pull requests one page only on release", () => {
    const loadOlder = vi.fn();
    const items = [makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })];
    render(<TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />);
    const viewport = screen.getByTestId("scroll-viewport");
    Object.defineProperty(viewport, "scrollHeight", { value: 1200, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    viewport.scrollTop = 100;
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(loadOlder).not.toHaveBeenCalled();
    viewport.scrollTop = 0;
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(loadOlder).not.toHaveBeenCalled();
    historyWheel(viewport, -48, false);
    expect(screen.getByTestId("load-older-spinner").dataset.state).toBe("pulling");
    expect(screen.getByTestId("load-older-spinner").querySelector(".animate-spin")).not.toBeNull();
    historyWheel(viewport, -48, false);
    expect(screen.getByTestId("load-older-spinner").dataset.state).toBe("ready");
    expect(loadOlder).not.toHaveBeenCalled();
    historyWheel(viewport, -20, true);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    // meiva:note
    // The released pull is the reader's own read — the only origin that may
    // present the loading strip.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/history-paging.meiva.okf.md
    expect(loadOlder).toHaveBeenCalledWith("reader");
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    historyWheel(viewport, -100, true);
    expect(loadOlder).toHaveBeenCalledTimes(1);
  });
  it("does not auto-load without older pages, and not while a load is already in flight (task #4186)", () => {
    const loadOlder = vi.fn();
    const items = [makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })];
    const { rerender } = render(<TimelineView items={items} onLoadOlder={loadOlder} />);
    const viewport = screen.getByTestId("scroll-viewport");
    viewport.scrollTop = 0;
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(loadOlder).not.toHaveBeenCalled();
    rerender(<TimelineView items={items} hasMoreOlder loadingOlder onLoadOlder={loadOlder} />);
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    expect(loadOlder).not.toHaveBeenCalled();
  });
  it("a failed load releases the anchor so the next deliberate pull can retry (QA #3031)", () => {
    const loadOlder = vi.fn();
    const items = [makeItem({ item_id: "10.0", kind: "agent_chat", payload: "ten" })];
    const { rerender } = render(<TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />);
    const viewport = screen.getByTestId("scroll-viewport");
    Object.defineProperty(viewport, "scrollHeight", { value: 1200, configurable: true });
    Object.defineProperty(viewport, "clientHeight", { value: 600, configurable: true });
    viewport.scrollTop = 100;
    viewport.dispatchEvent(new Event("scroll"));
    viewport.scrollTop = 0;
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    pullOlderAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(1);
    rerender(<TimelineView items={items} hasMoreOlder loadingOlder onLoadOlder={loadOlder} />);
    rerender(<TimelineView items={items} hasMoreOlder onLoadOlder={loadOlder} />);
    viewport.scrollTop = 80;
    act(() => { viewport.dispatchEvent(new Event("scrollend")); });
    viewport.scrollTop = 240;
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    viewport.scrollTop = 0;
    act(() => {
      viewport.dispatchEvent(new Event("scroll"));
    });
    pullOlderAtTop(viewport);
    expect(loadOlder).toHaveBeenCalledTimes(2);
  });
});

describe("ItemView: inbound_chat images", () => {
  it("renders an <img> per image url on a multimodal inbound", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: "User:\n\nwhat is this",
            images: ["/api/agents/7/uploads/shot.png"],
          }),
        ]}
      />,
    );
    const img = screen.getByAltText("attached image");
    expect(img.getAttribute("src")).toContain("/api/agents/7/uploads/shot.png");
  });
  it("attach item renders data-URI images raw (no API_BASE prefix)", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "attach",
            payload: "Files attached during this turn:\n- [1] render.png (image/png, 1.2 KiB)",
            images: ["data:image/png;base64,iVBORw0KGgo="],
          }),
        ]}
      />,
    );
    fireEvent.click(screen.getByTestId("turn-toggle"));
    const img = screen.getByAltText("attached image");
    expect(img.getAttribute("src")).toBe("data:image/png;base64,iVBORw0KGgo=");
    expect(screen.getByText(/render.png/)).toBeTruthy();
  });
  it("image-only inbound hides the [image] placeholder text but shows the thumbnail", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            kind: "inbound_chat",
            source: "user",
            payload: "User:\n\n[image]",
            images: ["/api/agents/7/uploads/a.png"],
          }),
        ]}
      />,
    );
    expect(screen.getByAltText("attached image")).toBeTruthy();
    expect(screen.queryByText("[image]")).toBeNull();
  });

  it("renders uploaded file cards + raster image thumbnails on system:upload, hides [files] companion, and falls back on image 404", () => {
    render(
      <TimelineView
        items={[
          makeItem({
            item_id: "10.0",
            kind: "inbound_chat",
            source: "system:upload",
            payload: "system:upload:\n\nFiles uploaded with the next message (saved to /tmp/b):",
            files: [
              {
                name: "diagram.png",
                size: 4096,
                content_type: "image/png",
                url: "/api/keyed/v1/agents/7/uploads/b/objects/0",
              },
              {
                name: "spec.pdf",
                size: 8192,
                content_type: "application/pdf",
                url: "/api/keyed/v1/agents/7/uploads/b/objects/1",
              },
            ],
          }),
          makeItem({
            item_id: "11.0",
            kind: "inbound_chat",
            source: "user",
            payload: "user [2026-11-01]:\n\n[files]",
          }),
        ]}
      />,
    );
    expect(screen.getByText("diagram.png")).toBeTruthy();
    expect(screen.getByText("spec.pdf")).toBeTruthy();
    expect(screen.queryByText("[files]")).toBeNull();
    expect(screen.queryByText(/Files uploaded with the next message/)).toBeNull();
    const img = screen.getByAltText("diagram.png");
    expect(img.getAttribute("src")).toContain("/api/keyed/v1/agents/7/uploads/b/objects/0");
    fireEvent.error(img);
    expect(screen.queryByAltText("diagram.png")).toBeNull();
    expect(screen.getByText("diagram.png")).toBeTruthy();
  });
});

describe("ItemView: attach interleaving + lightbox (user 2026-08-27)", () => {
  const NOTICE = "Files attached during this turn:";
  const LINE1 = '- [1] screen.png (image/png, 12.3 KiB) — "shot A"';
  const LINE2 = '- [2] chart.png (image/png, 45.6 KiB) — "chart B"';
  const IMG1 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAAB";
  const IMG2 = "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAIAAAAB";
  const payload = `${NOTICE}\n${LINE1}\n${LINE2}`;
  const imgs = () => screen.getAllByAltText("attached image");
  const orderOf = (el: Element) => {
    const all = Array.from(document.body.querySelectorAll("*"));
    return all.indexOf(el);
  };
  const renderAttach = (item: ReturnType<typeof makeItem>) => {
    render(<TimelineView items={[item]} />);
    fireEvent.click(screen.getByTestId("turn-toggle"));
  };
  it("interleaves label line → thumbnail → label line → thumbnail", () => {
    renderAttach(
      makeItem({
        kind: "attach",
        payload,
        images: [IMG1, IMG2],
        image_captions: [LINE1, LINE2],
      }),
    );
    const line1 = screen.getByText(LINE1);
    const line2 = screen.getByText(LINE2);
    const [img1, img2] = imgs();
    expect(orderOf(line1)).toBeLessThan(orderOf(img1));
    expect(orderOf(img1)).toBeLessThan(orderOf(line2));
    expect(orderOf(line2)).toBeLessThan(orderOf(img2));
    expect(document.body.querySelector('a[href^="data:image"]')).toBeNull();
  });
  it("renders a skipped (no-image) line in place without shifting alignment", () => {
    const skipped = "- [2] notes.pdf (application/pdf, 2.0 KiB) — not delivered: your model cannot receive pdf";
    renderAttach(
      makeItem({
        kind: "attach",
        payload: `${NOTICE}\n${LINE1}\n${skipped}\n${LINE2}`,
        images: [IMG1, IMG2],
        image_captions: [LINE1, LINE2],
      }),
    );
    const line1 = screen.getByText(LINE1);
    const sk = screen.getByText(skipped);
    const line2 = screen.getByText(LINE2);
    const [img1, img2] = imgs();
    expect(orderOf(line1)).toBeLessThan(orderOf(img1));
    expect(orderOf(img1)).toBeLessThan(orderOf(sk));
    expect(orderOf(sk)).toBeLessThan(orderOf(line2));
    expect(orderOf(line2)).toBeLessThan(orderOf(img2));
  });
  it("clicking a thumbnail opens the lightbox; backdrop click and Escape close it", () => {
    renderAttach(
      makeItem({
        kind: "attach",
        payload,
        images: [IMG1, IMG2],
        image_captions: [LINE1, LINE2],
      }),
    );
    expect(screen.queryByTestId("attach-lightbox")).toBeNull();
    expect(screen.getAllByTestId("attach-thumbnail")[0].querySelector("img")?.getAttribute("loading")).toBe("lazy");
    fireEvent.click(screen.getAllByTestId("attach-thumbnail")[0]);
    const lightbox = screen.getByTestId("attach-lightbox");
    expect(lightbox.querySelector("img")?.getAttribute("src")).toBe(IMG1);
    expect(lightbox.querySelector("img")?.getAttribute("loading")).toBe("lazy");
    fireEvent.click(lightbox);
    expect(screen.queryByTestId("attach-lightbox")).toBeNull();
    fireEvent.click(screen.getAllByTestId("attach-thumbnail")[1]);
    expect(screen.getByTestId("attach-lightbox")).toBeTruthy();
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("attach-lightbox")).toBeNull();
  });
  it("lightbox traps focus while open and returns it to the trigger on close", () => {
    renderAttach(
      makeItem({
        kind: "attach",
        payload,
        images: [IMG1, IMG2],
        image_captions: [LINE1, LINE2],
      }),
    );
    const [thumb0, thumb1] = screen.getAllByTestId("attach-thumbnail");
    fireEvent.click(thumb0);
    const lightbox = screen.getByTestId("attach-lightbox");
    expect(document.activeElement).toBe(lightbox);
    expect(document.body.style.overflow).toBe("hidden");
    fireEvent.keyDown(lightbox, { key: "Tab" });
    expect(document.activeElement).toBe(lightbox);
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByTestId("attach-lightbox")).toBeNull();
    expect(document.activeElement).toBe(thumb0);
    expect(document.body.style.overflow).toBe("");
    fireEvent.click(thumb1);
    expect(document.activeElement).toBe(screen.getByTestId("attach-lightbox"));
  });
  it("mounts the lightbox at document.body so the backdrop is not clipped by the contained timeline row", () => {
    renderAttach(
      makeItem({
        kind: "attach",
        payload,
        images: [IMG1, IMG2],
        image_captions: [LINE1, LINE2],
      }),
    );
    fireEvent.click(screen.getAllByTestId("attach-thumbnail")[0]);
    const lightbox = screen.getByTestId("attach-lightbox");
    expect(document.body.contains(lightbox)).toBe(true);
    expect(lightbox.closest("[data-item-id]")).toBeNull();
    expect(lightbox.className).toContain("fixed inset-0 z-50");
  });
  it("enlarged image is capped at 80% of the viewport (contain, no upscale)", () => {
    renderAttach(
      makeItem({
        kind: "attach",
        payload,
        images: [IMG1, IMG2],
        image_captions: [LINE1, LINE2],
      }),
    );
    fireEvent.click(screen.getAllByTestId("attach-thumbnail")[0]);
    const img = screen.getByTestId("attach-lightbox").querySelector("img");
    expect(img?.className).toContain("max-h-[80vh]");
    expect(img?.className).toContain("max-w-[80vw]");
    expect(img?.className).toContain("object-contain");
  });
  it("reserves the thumbnail's max box so lazy img load never shifts layout (QA review-955)", () => {
    renderAttach(
      makeItem({
        kind: "attach",
        payload,
        images: [IMG1],
        image_captions: [LINE1],
      }),
    );
    const thumb = screen.getByTestId("attach-thumbnail");
    expect(thumb.className).toContain("relative");
    expect(thumb.className).toContain("h-48");
    expect(thumb.className).toContain("w-64");
    const img = thumb.querySelector("img");
    expect(img).not.toBeNull();
    expect(img!.className).toContain("absolute");
    expect(img!.className).toContain("inset-0");
    expect(img!.className).toContain("m-auto");
    expect(img!.className).toContain("max-h-48");
    expect(img!.className).toContain("max-w-[16rem]");
    expect(img!.className).toContain("object-contain");
  });
  it("legacy attach (images without image_captions) still renders thumbnails without navigation", () => {
    renderAttach(
      makeItem({
        kind: "attach",
        payload,
        images: [IMG1, IMG2],
        image_captions: null,
      }),
    );
    expect(imgs()).toHaveLength(2);
    expect(document.body.querySelector('a[href^="data:image"]')).toBeNull();
  });
});
