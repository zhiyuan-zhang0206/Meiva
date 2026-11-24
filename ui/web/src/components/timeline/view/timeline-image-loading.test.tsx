import { act, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { createLazyChunk } from "@/components/content/lazy-chunk";
import { ChatMarkdownRendererProvider, preloadChatMarkdown } from "@/components/content/markdown";
import { MarkdownZoomableImage } from "@/components/content/zoomable-image";
import {
  INITIAL_IMAGE_GATE_TIMEOUT_MS,
  ITEM_IMAGE_BATCH_TIMEOUT_MS,
} from "../model/image-loading";
import {
  cleanupTimelineTest,
  makeItem,
  render,
} from "./timeline-test-mock";

vi.mock("@/components/ui/scroll-area", async () => ({
  ScrollArea: (await import("./timeline-test-mock")).ScrollAreaMock,
}));

import { TimelineView } from "..";

beforeAll(async () => {
  await preloadChatMarkdown();
});

afterEach(() => {
  vi.useRealTimers();
  cleanupTimelineTest();
});

function fireImageLoad(img: HTMLImageElement, width: number, height: number) {
  Object.defineProperty(img, "naturalWidth", { value: width, configurable: true });
  Object.defineProperty(img, "naturalHeight", { value: height, configurable: true });
  Object.defineProperty(img, "complete", { value: true, configurable: true });
  fireEvent.load(img);
}

describe("TimelineView image placeholders and zero-jitter loading", () => {
  it("reserves exact scaled dimensions and shows a Spinner for known-size thumbnails and HTML Markdown images", () => {
    const { container } = render(
      <TimelineView
        threadKey="7"
        items={[
          makeItem({
            item_id: "1.0",
            kind: "inbound_chat",
            source: "user",
            payload: "see screenshot",
            images: ["/api/agents/7/uploads/a/0"],
            image_dimensions: [{ width: 800, height: 400 }],
          }),
          makeItem({
            item_id: "2.0",
            kind: "agent_chat",
            payload: '<img src="https://example.com/chart.png" alt="chart" width="400" height="200">',
          }),
        ]}
      />,
    );

    const thumb = screen.getByTestId("attach-thumbnail");
    expect(thumb.style.width).toBe("256px");
    expect(thumb.style.aspectRatio).toBe("800 / 400");
    expect(thumb.querySelector('[data-testid="timeline-image-placeholder"]')).not.toBeNull();
    const thumbImg = thumb.querySelector("img")!;
    expect(thumbImg.getAttribute("loading")).toBe("eager");
    expect(thumbImg.className).toContain("opacity-0");

    fireImageLoad(thumbImg, 800, 400);
    expect(thumb.querySelector('[data-testid="timeline-image-placeholder"]')).toBeNull();
    expect(thumb.style.width).toBe("256px");
    expect(thumb.style.aspectRatio).toBe("800 / 400");
    expect(thumbImg.className).toContain("opacity-100");

    const mdSlot = container.querySelector<HTMLElement>('[data-testid="timeline-image-slot"]')!;
    expect(mdSlot.tagName).toBe("SPAN");
    expect(mdSlot.className).toContain("inline-block");
    expect(mdSlot.className).toContain("max-w-full");
    expect(mdSlot.className).toContain("bg-muted/30");
    expect(mdSlot.style.width).toBe("400px");
    expect(mdSlot.style.aspectRatio).toBe("400 / 200");
    expect(mdSlot.querySelector('[data-testid="timeline-image-placeholder"]')).not.toBeNull();
    const mdImg = mdSlot.querySelector("img")!;
    expect(mdImg.className).toContain("absolute");
    expect(mdImg.className).toContain("inset-0");
    fireImageLoad(mdImg, 400, 200);
    expect(mdSlot.querySelector('[data-testid="timeline-image-placeholder"]')).toBeNull();
    expect(mdSlot.className).not.toContain("bg-muted/30");
    expect(mdSlot.style.width).toBe("400px");
    expect(mdSlot.style.aspectRatio).toBe("400 / 200");
  });

  it("keeps unknown-size legacy thumbnails inside a fixed 256x192 box on first mount and uses cached dimensions on remount", () => {
    const item = makeItem({
      item_id: "1.0",
      kind: "inbound_chat",
      source: "user",
      payload: "legacy upload",
      images: ["/api/agents/7/uploads/legacy/0"],
    });
    const first = render(<TimelineView threadKey="7" items={[item]} />);
    const thumb1 = screen.getByTestId("attach-thumbnail");
    expect(thumb1.className).toContain("h-48");
    expect(thumb1.className).toContain("w-64");
    expect(thumb1.style.width).toBe("");
    expect(thumb1.querySelector('[data-testid="timeline-image-placeholder"]')).not.toBeNull();

    fireImageLoad(thumb1.querySelector("img")!, 800, 400);
    expect(thumb1.querySelector('[data-testid="timeline-image-placeholder"]')).toBeNull();
    expect(thumb1.className).toContain("h-48");
    expect(thumb1.className).toContain("w-64");
    expect(thumb1.style.width).toBe("");
    first.unmount();

    render(<TimelineView threadKey="7" items={[item]} />);
    const thumb2 = screen.getByTestId("attach-thumbnail");
    expect(thumb2.className.split(/\s+/)).not.toContain("h-48");
    expect(thumb2.className.split(/\s+/)).not.toContain("w-64");
    expect(thumb2.style.width).toBe("256px");
    expect(thumb2.style.aspectRatio).toBe("800 / 400");
  });

  it("holds the initial cold-load reveal until all unknown-size Markdown images settle, then reveals once", () => {
    const { container } = render(
      <TimelineView
        threadKey="10"
        items={[
          makeItem({
            item_id: "1.0",
            kind: "agent_chat",
            payload:
              "First ![one](https://example.com/one.png)\n\nSecond ![two](https://example.com/two.png)",
          }),
        ]}
      />,
    );

    const column = container.querySelector<HTMLElement>("[data-timeline-column]")!;
    expect(column.style.visibility).toBe("hidden");
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();

    const imgs = Array.from(container.querySelectorAll("img"));
    expect(imgs).toHaveLength(2);
    expect(imgs[0].getAttribute("loading")).toBe("eager");
    expect(imgs[1].getAttribute("loading")).toBe("eager");

    fireImageLoad(imgs[0], 500, 300);
    expect(column.style.visibility).toBe("hidden");
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();

    fireImageLoad(imgs[1], 600, 400);
    expect(column.style.visibility).toBe("");
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
    const slots = container.querySelectorAll<HTMLElement>('[data-testid="timeline-image-slot"]');
    expect(slots).toHaveLength(2);
    expect(slots[0].style.width).toBe("500px");
    expect(slots[1].style.width).toBe("600px");
  });

  it("holds the initial reveal while ChatMarkdown chunk is pending for image-bearing content", async () => {
    type ChunkRenderer = Parameters<typeof ChatMarkdownRendererProvider>[0]["value"] extends {
      peek: () => infer R | null;
    }
      ? R
      : never;
    const FakeImageMarkdown = (() => (
      <div className="chat-md">
        <p>
          <MarkdownZoomableImage src="https://example.com/lazy-chunk.png" alt="lazy" />
        </p>
      </div>
    )) as unknown as ChunkRenderer;

    let resolveRenderer!: (value: ChunkRenderer) => void;
    const deferredChunk = createLazyChunk<ChunkRenderer>({
      name: "test-deferred-markdown",
      importModule: () =>
        new Promise<ChunkRenderer>((resolve) => {
          resolveRenderer = resolve;
        }),
    });

    const { container } = render(
      <ChatMarkdownRendererProvider value={deferredChunk}>
        <TimelineView
          threadKey="14"
          items={[
            makeItem({
              item_id: "1.0",
              kind: "agent_chat",
              payload: "Look at ![lazy](https://example.com/lazy-chunk.png)",
            }),
          ]}
        />
      </ChatMarkdownRendererProvider>,
    );

    const column = container.querySelector<HTMLElement>("[data-timeline-column]")!;
    expect(screen.getByTestId("chat-markdown-plain")).toBeTruthy();
    expect(column.style.visibility).toBe("hidden");
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();

    await act(async () => {
      resolveRenderer(FakeImageMarkdown);
      await Promise.resolve();
    });

    await waitFor(() => expect(container.querySelector("img")).not.toBeNull());
    expect(column.style.visibility).toBe("hidden");
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();

    fireImageLoad(container.querySelector("img")!, 480, 320);
    expect(column.style.visibility).toBe("");
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
  });

  it("reveals initial cold load after the safety timeout even if an unknown-size image hangs", () => {
    vi.useFakeTimers();
    const { container } = render(
      <TimelineView
        threadKey="11"
        items={[
          makeItem({
            item_id: "1.0",
            kind: "agent_chat",
            payload: "Hanging ![broken](https://example.com/hang.png)",
          }),
        ]}
      />,
    );

    const column = container.querySelector<HTMLElement>("[data-timeline-column]")!;
    expect(column.style.visibility).toBe("hidden");
    expect(screen.getByTestId("cold-load-spinner")).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(INITIAL_IMAGE_GATE_TIMEOUT_MS + 10);
    });

    expect(column.style.visibility).toBe("");
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();
  });

  it("batches unknown-size Markdown images on newly arrived messages so all reveal together on one frame", () => {
    const initialItems = [
      makeItem({
        item_id: "1.0",
        kind: "agent_chat",
        payload: "Ready conversation",
      }),
    ];
    const { container, rerender } = render(<TimelineView threadKey="12" items={initialItems} />);

    const column = container.querySelector<HTMLElement>("[data-timeline-column]")!;
    expect(column.style.visibility).toBe("");
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();

    rerender(
      <TimelineView
        threadKey="12"
        items={[
          ...initialItems,
          makeItem({
            item_id: "2.0",
            kind: "agent_chat",
            payload:
              "Batch ![a](https://example.com/batch-a.png)\n\n![b](https://example.com/batch-b.png)",
          }),
        ]}
      />,
    );

    expect(column.style.visibility).toBe("");
    expect(screen.queryByTestId("cold-load-spinner")).toBeNull();

    expect(screen.getAllByTestId("timeline-image-batch-pending")).toHaveLength(2);
    expect(screen.queryAllByTestId("timeline-image-slot")).toHaveLength(0);

    const imgs = Array.from(container.querySelectorAll("img"));
    expect(imgs).toHaveLength(2);

    fireImageLoad(imgs[0], 420, 280);
    expect(screen.getAllByTestId("timeline-image-batch-pending")).toHaveLength(2);
    expect(screen.queryAllByTestId("timeline-image-slot")).toHaveLength(0);

    fireImageLoad(imgs[1], 640, 360);
    expect(screen.queryAllByTestId("timeline-image-batch-pending")).toHaveLength(0);
    const slots = screen.getAllByTestId("timeline-image-slot");
    expect(slots).toHaveLength(2);
    expect(slots[0].style.width).toBe("420px");
    expect(slots[0].style.aspectRatio).toBe("420 / 280");
    expect(slots[1].style.width).toBe("640px");
    expect(slots[1].style.aspectRatio).toBe("640 / 360");
  });

  it("releases a post-reveal item batch after the safety timeout if one image never finishes", () => {
    vi.useFakeTimers();
    const initialItems = [
      makeItem({ item_id: "1.0", kind: "agent_chat", payload: "Initial message" }),
    ];
    const { container, rerender } = render(<TimelineView threadKey="13" items={initialItems} />);

    rerender(
      <TimelineView
        threadKey="13"
        items={[
          ...initialItems,
          makeItem({
            item_id: "2.0",
            kind: "agent_chat",
            payload:
              "Fast ![ok](https://example.com/ok.png)\n\nStalled ![slow](https://example.com/slow.png)",
          }),
        ]}
      />,
    );

    const imgs = Array.from(container.querySelectorAll("img"));
    fireImageLoad(imgs[0], 300, 200);
    expect(screen.getAllByTestId("timeline-image-batch-pending")).toHaveLength(2);

    act(() => {
      vi.advanceTimersByTime(ITEM_IMAGE_BATCH_TIMEOUT_MS + 10);
    });

    expect(screen.queryAllByTestId("timeline-image-batch-pending")).toHaveLength(0);
    const slots = screen.getAllByTestId("timeline-image-slot");
    expect(slots).toHaveLength(2);
    expect(slots[0].style.width).toBe("300px");
  });
});
