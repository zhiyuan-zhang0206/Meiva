import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { PendingStrip, userPending } from "./pending-strip";
import type { DeliveredUploadStatus, PendingInbound, Quote } from "@/lib/contracts/types";

afterEach(cleanup);

function item(over: Partial<PendingInbound> & { id: number }): PendingInbound {
  return {
    source: "user",
    sender: "user",
    content: "hello",
    images: null,
    created_at: "2026-05-28T00:00:00Z",
    ...over,
  };
}

function quote(over: Partial<Quote> = {}): Quote {
  return {
    source_agent_id: 7,
    source_item_id: "item-1",
    author: "Agent 7",
    text: "a selected passage",
    ...over,
  };
}

function batch(over: Partial<DeliveredUploadStatus> = {}): DeliveredUploadStatus {
  return {
    batch_id: "a".repeat(32),
    state: "pending",
    reason: null,
    directory: "/tmp/a",
    message_text: "analyze this",
    files: [
      {
        ordinal: 0,
        name: "data.csv",
        staged_id: "b".repeat(32),
        size: 128,
        sha256: "c".repeat(64),
        content_type: "text/csv",
      },
    ],
    inbound_id: null,
    source: { machine: "m1", home: "/tmp" },
    target: { machine: "m1", home: "/tmp" },
    attempts: 0,
    next_attempt_at: null,
    ...over,
  };
}

function mixedQueue(): PendingInbound[] {
  return [
    item({ id: 1, content: "check the logs" }),
    item({ id: 2, source: "agent:3", sender: "agent", content: "peer asks for a status" }),
    item({ id: 3, source: "system", sender: "system", content: "[system] compact now" }),
  ];
}

describe("userPending", () => {
  it("keeps the rows the user caused, including upload batches", () => {
    const queued = userPending([
      item({ id: 1 }),
      item({ id: 2, source: "user", sender: "user" }),
      item({
        id: 3,
        source: "system:upload",
        sender: "system",
        files: [{ name: "report.pdf", size: 1024, content_type: "application/pdf", url: "/api/keyed/v1/agents/7/uploads/b/objects/0" }],
      }),
    ]);
    expect(queued.map((p) => p.id)).toEqual([1, 2, 3]);
  });

  it("drops agent, automation and system rows", () => {
    const queued = userPending([
      item({ id: 1, source: "agent:3", sender: "agent" }),
      item({ id: 2, source: "schedule:7", sender: "automation" }),
      item({ id: 3, source: "watcher:5", sender: "automation" }),
      item({ id: 4, source: "system", sender: "system" }),
    ]);
    expect(queued).toEqual([]);
  });
});

describe("PendingStrip", () => {
  it("renders nothing when empty", () => {
    const { container } = render(<PendingStrip items={[]} />);
    expect(container.firstChild).toBeNull();
  });

  it("shows only the user-originated pending message of a mixed queue", () => {
    render(<PendingStrip items={mixedQueue()} />);
    expect(screen.getByText("1 pending")).toBeTruthy();
    expect(screen.getByText("check the logs")).toBeTruthy();
    expect(screen.queryByText("peer asks for a status")).toBeNull();
    expect(screen.queryByText("[system] compact now")).toBeNull();
  });

  it("renders nothing when the queue holds no user message", () => {
    const { container } = render(
      <PendingStrip
        items={[
          item({ id: 1, source: "agent:3", sender: "agent", content: "peer asks for a status" }),
          item({ id: 2, source: "schedule:7", sender: "automation", content: "wake" }),
        ]}
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("shows count + each item's content", () => {
    render(
      <PendingStrip
        items={[
          item({ id: 1, content: "check the logs" }),
          item({ id: 2, content: "here is the result" }),
        ]}
      />,
    );
    expect(screen.getByText("2 pending")).toBeTruthy();
    expect(screen.getByText("check the logs")).toBeTruthy();
    expect(screen.getByText("here is the result")).toBeTruthy();
  });

  it("renders a thumbnail per image on a queued multimodal message", () => {
    render(
      <PendingStrip
        items={[
          item({
            id: 1,
            content: "look at this",
            images: ["/api/agents/7/uploads/a.png", "/api/agents/7/uploads/b.png"],
          }),
        ]}
      />,
    );
    const thumbs = screen.getAllByRole("img");
    expect(thumbs).toHaveLength(2);
    expect(thumbs[0].getAttribute("src")).toContain("/api/agents/7/uploads/a.png");
    expect(thumbs[1].getAttribute("src")).toContain("/api/agents/7/uploads/b.png");
    expect(thumbs[0].getAttribute("alt")).toBe("pending image");
    expect(screen.getByText("look at this")).toBeTruthy();
  });

  it("renders no thumbnail and keeps the text for a text-only message", () => {
    render(<PendingStrip items={[item({ id: 1, content: "just text" })]} />);
    expect(screen.queryAllByRole("img")).toHaveLength(0);
    expect(screen.getByText("just text")).toBeTruthy();
  });

  it("suppresses the [image] placeholder when thumbnails render", () => {
    const { rerender } = render(
      <PendingStrip items={[item({ id: 1, content: "[image]", images: ["/api/agents/7/uploads/a.png"] })]} />,
    );
    expect(screen.queryByText("[image]")).toBeNull();
    rerender(<PendingStrip items={[item({ id: 1, content: "[image]" })]} />);
    expect(screen.getByText("[image]")).toBeTruthy();
  });

  it("caps and centers the root container with px-4 so its inner border-t matches the composer width", () => {
    render(<PendingStrip items={[item({ id: 1, content: "queued" })]} maxWidthCss="768px" />);
    const root = screen.getByTestId("pending-strip");
    expect(root.style.maxWidth).toBe("768px");
    expect(root.className.split(" ")).toEqual(expect.arrayContaining(["mx-auto", "w-full", "px-4"]));
    const inner = root.firstElementChild as HTMLElement;
    expect(inner.style.maxWidth).toBe("");
    expect(inner.className.split(" ")).toContain("border-t");
  });

  it("renders file chips for system:upload and suppresses the [files] placeholder on the companion user row", () => {
    render(
      <PendingStrip
        items={[
          item({
            id: 10,
            source: "system:upload",
            sender: "system",
            content: "Files uploaded with the next message (saved to /tmp/b):",
            files: [
              {
                name: "report.pdf",
                size: 2048,
                content_type: "application/pdf",
                url: "/api/keyed/v1/agents/7/uploads/b/objects/0",
              },
            ],
          }),
          item({ id: 11, source: "user", sender: "user", content: "[files]" }),
        ]}
      />,
    );
    expect(screen.getByText("report.pdf")).toBeTruthy();
    expect(screen.queryByText("[files]")).toBeNull();
    expect(screen.queryByText(/Files uploaded with the next message/)).toBeNull();
  });

  it("renders an in-flight delivering upload batch", () => {
    render(
      <PendingStrip
        items={[]}
        deliveringBatch={{
          batch_id: "a".repeat(32),
          state: "pending",
          reason: null,
          directory: "/tmp/a",
          message_text: "analyze this",
          files: [
            {
              ordinal: 0,
              name: "data.csv",
              staged_id: "b".repeat(32),
              size: 128,
              sha256: "c".repeat(64),
              content_type: "text/csv",
            },
          ],
          inbound_id: null,
          source: { machine: "m1", home: "/tmp" },
          target: { machine: "m1", home: "/tmp" },
          attempts: 0,
          next_attempt_at: null,
        }}
      />,
    );
    expect(screen.getByTestId("pending-delivering-batch")).toBeTruthy();
    expect(screen.getByText("analyze this")).toBeTruthy();
    expect(screen.getByText("data.csv")).toBeTruthy();
  });

  it("renders a quote card above a queued message", () => {
    render(<PendingStrip items={[item({ id: 1, content: "explain this", quote: quote() })]} />);
    const card = screen.getByTestId("pending-quote");
    expect(card.textContent).toContain("Quoted · Agent 7");
    expect(card.textContent).toContain("a selected passage");
    expect(screen.getByText("explain this")).toBeTruthy();
    const li = card.closest("li") as HTMLElement;
    expect(li.className.split(" ")).toContain("flex-col");
    expect(li.firstElementChild).toBe(card);
  });

  it("renders the quote card on an in-flight delivering batch", () => {
    render(<PendingStrip items={[]} deliveringBatch={batch({ quote: quote({ text: "from the batch" }) })} />);
    const row = screen.getByTestId("pending-delivering-batch");
    const card = screen.getByTestId("pending-quote");
    expect(row.contains(card)).toBe(true);
    expect(card.textContent).toContain("from the batch");
    expect(screen.getByText("analyze this")).toBeTruthy();
    expect(screen.getByText("data.csv")).toBeTruthy();
  });

  it("keeps the quote card with text, file chips and thumbnails of the same message", () => {
    render(
      <PendingStrip
        items={[
          item({
            id: 1,
            content: "look at this",
            quote: quote(),
            images: ["/api/agents/7/uploads/a.png"],
            files: [{ name: "report.pdf", size: 2048, content_type: "application/pdf", url: "/api/keyed/v1/agents/7/uploads/b/objects/0" }],
          }),
        ]}
      />,
    );
    expect(screen.getAllByTestId("pending-quote")).toHaveLength(1);
    expect(screen.getByText("look at this")).toBeTruthy();
    expect(screen.getByText("report.pdf")).toBeTruthy();
    expect(screen.getAllByRole("img")).toHaveLength(1);
  });

  it("wraps a long multi-line quote with line-clamp-2 and anywhere-wrapping", () => {
    const text = `${"x".repeat(300)}\nsecond line\nthird line`;
    render(<PendingStrip items={[item({ id: 1, quote: quote({ text }) })]} />);
    const body = screen.getByTestId("pending-quote").querySelector("p") as HTMLElement;
    expect(body.textContent).toBe(text);
    const classes = body.className.split(" ");
    expect(classes).toEqual(expect.arrayContaining(["line-clamp-2", "whitespace-pre-wrap", "[overflow-wrap:anywhere]"]));
  });

  it("renders no quote card and keeps the compact row when there is no quote", () => {
    render(
      <PendingStrip
        items={[item({ id: 1, content: "plain", quote: null }), item({ id: 2, content: "plain too" })]}
        deliveringBatch={batch()}
      />,
    );
    expect(screen.queryByTestId("pending-quote")).toBeNull();
    const li = screen.getByText("plain").closest("li") as HTMLElement;
    expect(li.className.split(" ")).not.toContain("flex-col");
    expect(li.className.split(" ")).toEqual(expect.arrayContaining(["items-center", "pl-4"]));
  });
});
