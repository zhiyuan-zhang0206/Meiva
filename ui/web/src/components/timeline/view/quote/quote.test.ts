import { createElement, useRef } from "react";
import { act, cleanup, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { fakeBrowser } from "@/lib/browser/testing";
import type { BackendTimelineItem } from "@/lib/contracts/types";
import { renderWithBrowser } from "@/test-support/render";
import {
  clampQuotePopoverPosition,
  QUOTE_POPOVER_ESTIMATED_HEIGHT,
  QUOTE_POPOVER_ESTIMATED_WIDTH,
  quoteAnchorRect,
  quoteFromRange,
  QuoteSelection,
} from "./quote";

afterEach(cleanup);

function makeRect(left: number, top: number, right: number, bottom: number): DOMRect {
  return {
    x: left,
    y: top,
    left,
    top,
    right,
    bottom,
    width: right - left,
    height: bottom - top,
    toJSON: () => ({}),
  };
}

function makeRectList(rects: DOMRect[]): DOMRectList {
  return Object.assign([...rects], {
    item: (index: number) => rects[index] ?? null,
  });
}

const item: BackendTimelineItem = {
  item_id: "4.0", kind: "agent_chat", payload: "hello exact\nselection", source: null,
  created_at: "2026-11-17T10:00:00Z", inbound_id: null, source_message_id: "durable-message",
  source_block_idx: 0, show_timestamp: true, user_facing: true,
};
function fixture() {
  const root = document.createElement("div");
  root.innerHTML = '<div class="timeline-item" data-item-id="4.0"><div data-quote-body>hello exact\nselection</div></div><div class="timeline-item" data-item-id="5.0"><div data-quote-body>other message</div></div><textarea>draft</textarea>';
  const text = root.querySelector("[data-quote-body]")!.firstChild!;
  const range = document.createRange(); range.setStart(text, 6); range.setEnd(text, text.textContent!.length);
  return { root, range };
}
describe("selected text quotes", () => {
  it("retains exact selected text and durable provenance", () => {
    const { root, range } = fixture();
    expect(quoteFromRange(range, root, [item], 12)).toMatchObject({ text: "exact\nselection", source_agent_id: 12, source_message_id: "durable-message", source_block_idx: 0 });
  });
  it("quotes a marker body fragment with stable provenance while excluding its title", () => {
    const { root, range } = fixture();
    const marker: BackendTimelineItem = { ...item, kind: "system_marker", source: "system_note", source_message_id: "durable-marker" };
    root.querySelector(".timeline-item")!.insertAdjacentHTML("afterbegin", "<button>Error title</button>");
    expect(quoteFromRange(range, root, [marker], 12)).toMatchObject({ text: "exact\nselection", source_message_id: "durable-marker", source_block_idx: 0 });
    range.selectNodeContents(root.querySelector("button")!);
    expect(quoteFromRange(range, root, [marker], 12)).toBeNull();
  });
  it("rejects cross-message, input and partial ranges", () => {
    const { root, range } = fixture();
    expect(quoteFromRange(range, root, [{ ...item, partial: true }], 12)).toBeNull();
    expect(quoteFromRange(range, root, [{ ...item, source_message_id: null }], 12)?.text).toBe("exact\nselection");
    range.setEnd(root.querySelectorAll("[data-quote-body]")[1].firstChild!, 3);
    expect(quoteFromRange(range, root, [item], 12)).toBeNull();
    range.selectNodeContents(root.querySelector("textarea")!);
    expect(quoteFromRange(range, root, [item], 12)).toBeNull();
  });
});

describe("quoteAnchorRect and bottom-right popover positioning", () => {
  it("anchors single-line mid-row selection at the last character rect instead of first character left", () => {
    const singleLineRect = makeRect(220, 120, 360, 142);
    const resolved = quoteAnchorRect({
      getClientRects: () => makeRectList([singleLineRect]),
      getBoundingClientRect: () => singleLineRect,
    });
    expect(resolved.right).toBe(360);
    expect(resolved.bottom).toBe(142);
    expect(
      clampQuotePopoverPosition({
        anchorX: resolved.right - 100,
        anchorY: resolved.bottom - 50,
        outerWidth: 600,
        outerHeight: 400,
      }),
    ).toEqual({ left: 264, top: 98 });
  });

  it("anchors multi-line selection at the second line's last character rect instead of the full block bounding box", () => {
    const firstLine = makeRect(420, 120, 680, 142);
    const secondLine = makeRect(140, 146, 290, 168);
    const trailingZero = makeRect(290, 168, 290, 168);
    const fullBounding = makeRect(140, 120, 680, 168);

    const resolved = quoteAnchorRect({
      getClientRects: () => makeRectList([firstLine, secondLine, trailingZero]),
      getBoundingClientRect: () => fullBounding,
    });
    expect(resolved).toBe(secondLine);
    expect(resolved.right).toBe(290);
    expect(resolved.bottom).toBe(168);

    const fallback = quoteAnchorRect({
      getBoundingClientRect: () => fullBounding,
    });
    expect(fallback).toBe(fullBounding);
  });

  it("clamps QuoteSelection popover cleanly inside the right and bottom edges when selecting at line end", () => {
    let activeRange: Range | null = null;
    const listeners = new Set<() => void>();
    const base = fakeBrowser();
    const browser = {
      ...base,
      selection: {
        read: () => activeRange,
        subscribe: (_root: Element, onChange: () => void) => {
          listeners.add(onChange);
          return () => {
            listeners.delete(onChange);
          };
        },
      },
    };
    const onQuote = vi.fn();
    const outerRect = makeRect(100, 50, 700, 450);

    function Harness() {
      const scopeRef = useRef<HTMLDivElement>(null);
      return createElement(
        "div",
        {
          ref: (node: HTMLDivElement | null) => {
            scopeRef.current = node;
            if (node) {
              node.getBoundingClientRect = () => outerRect;
            }
          },
        },
        createElement(
          "div",
          { className: "timeline-item", "data-item-id": "4.0" },
          createElement("div", { "data-quote-body": "" }, "hello exact\nselection"),
        ),
        createElement(QuoteSelection, {
          scope: scopeRef,
          items: [item],
          agentId: 12,
          onQuote,
        }),
      );
    }

    const { container } = renderWithBrowser(createElement(Harness), { browser });

    const textNode = container.querySelector("[data-quote-body]")!.firstChild!;
    const midLineRange = document.createRange();
    midLineRange.setStart(textNode, 0);
    midLineRange.setEnd(textNode, 5);
    midLineRange.getClientRects = () => makeRectList([makeRect(180, 110, 260, 132)]);
    midLineRange.getBoundingClientRect = () => makeRect(180, 110, 260, 132);
    activeRange = midLineRange;
    act(() => {
      listeners.forEach((notify) => notify());
    });

    const popover = screen.getByTestId("quote-selection-popover");
    expect(popover.style.left).toBe("164px");
    expect(popover.style.top).toBe("88px");

    const multiLineRange = document.createRange();
    multiLineRange.setStart(textNode, 6);
    multiLineRange.setEnd(textNode, textNode.textContent!.length);
    multiLineRange.getClientRects = () =>
      makeRectList([makeRect(450, 110, 690, 132), makeRect(140, 136, 310, 158)]);
    multiLineRange.getBoundingClientRect = () => makeRect(140, 110, 690, 158);
    activeRange = multiLineRange;
    act(() => {
      listeners.forEach((notify) => notify());
    });
    expect(popover.style.left).toBe("214px");
    expect(popover.style.top).toBe("114px");

    const rightEdgeRange = document.createRange();
    rightEdgeRange.setStart(textNode, 0);
    rightEdgeRange.setEnd(textNode, 11);
    rightEdgeRange.getClientRects = () => makeRectList([makeRect(520, 420, 696, 444)]);
    rightEdgeRange.getBoundingClientRect = () => makeRect(520, 420, 696, 444);
    activeRange = rightEdgeRange;
    act(() => {
      listeners.forEach((notify) => notify());
    });
    expect(popover.style.left).toBe(`${600 - QUOTE_POPOVER_ESTIMATED_WIDTH - 8}px`);
    expect(popover.style.top).toBe(`${400 - QUOTE_POPOVER_ESTIMATED_HEIGHT - 6}px`);
  });
});
