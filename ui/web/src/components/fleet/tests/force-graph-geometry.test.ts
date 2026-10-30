import { describe, expect, it } from "vitest";

import { hoverCardLeave, placeHoverCard, type HoverCardNodeBox } from "../force-graph-geometry";

// meiva:note
// The hover card's two decisions, stated as values: where the card goes and what a pointer leaving
// the node does next. Both used to be asserted through the board with a faked DOM box; with the
// decisions out here the cases read as the numbers a real canvas produced.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md

// meiva:note
// A node's page box (left edge, top edge, square side) as the placement takes it: the right edge
// anchors the beside/behind placements, the left edge the flip.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
function nodeBox(left: number, top: number): HoverCardNodeBox {
  return { x: left + 36, y: top, flipX: left, h: 36 };
}

describe("placeHoverCard", () => {
  it("sits beside a node with room to its right", () => {
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 600, height: 400 },
        node: nodeBox(100, 100),
        card: { width: 200, height: 100 },
        legend: null,
      }),
    ).toEqual({ left: 150, top: 114, maxHeight: "" });
  });

  it("flips to the node's left edge rather than cover it", () => {
    // meiva:note
    // QA #990: the flip anchors at the node's LEFT edge. Anchoring at the right edge parked the
    // card on the node it describes (~70% overlap at the canvas edge).
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 300, height: 400 },
        node: nodeBox(250, 100),
        card: { width: 200, height: 100 },
        legend: null,
      }),
    ).toEqual({ left: 36, top: 114, maxHeight: "" });
  });

  it("centres above a mid-band node when neither side has room", () => {
    // meiva:note
    // QA #990 on a 320x390 canvas: the card goes above the node and horizontally centred, clear of
    // the message band below it. Clamping to the left pad instead would cover the node.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 320, height: 390 },
        node: nodeBox(130, 180),
        card: { width: 200, height: 100 },
        legend: null,
      }),
    ).toEqual({ left: 48, top: 66, maxHeight: "" });
  });

  it("caps the height on the roomier side when the card fits neither way", () => {
    // meiva:note
    // The card is taller than both gaps around the node: pin it to the roomier side (above) and cap
    // it so the body scrolls instead of running over the node. Below space 156, above space 162.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 320, height: 390 },
        node: nodeBox(130, 180),
        card: { width: 200, height: 300 },
        legend: null,
      }),
    ).toEqual({ left: 48, top: 4, maxHeight: "162px" });
  });

  it("holds a tall card under the node, capped at the canvas floor", () => {
    // meiva:note
    // User report 2026-10-20: a tall card on a node low in the canvas used to hang past the bottom
    // edge, where the canvas clipped it. It stays below the node now and scrolls to the floor.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 900, height: 900 },
        node: nodeBox(100, 400),
        card: { width: 320, height: 509 },
        legend: null,
      }),
    ).toEqual({ left: 150, top: 414, maxHeight: "482px" });
  });

  it("stops above the legend when the card would bury it", () => {
    // meiva:note
    // 464 + 300 still fits the canvas, but the card's column crosses the legend's, and the legend is
    // the only hint of what the node colours mean: the card flips above the node instead.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 900, height: 900 },
        node: nodeBox(600, 450),
        card: { width: 320, height: 300 },
        legend: { left: 560, top: 700, width: 200, height: 30 },
      }),
    ).toEqual({ left: 266, top: 136, maxHeight: "" });
  });

  it("leaves the canvas floor alone when the card misses the legend's column", () => {
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 900, height: 900 },
        node: nodeBox(100, 400),
        card: { width: 320, height: 509 },
        legend: { left: 560, top: 700, width: 200, height: 30 },
      }),
    ).toEqual({ left: 150, top: 414, maxHeight: "482px" });
  });

  it("measures the node in the canvas's own frame", () => {
    // meiva:note
    // The page reports both boxes in viewport coordinates; the card is positioned inside the
    // canvas, so the canvas's origin is subtracted from the node and the legend alike.
    // meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
    expect(
      placeHoverCard({
        canvas: { left: 40, top: 20, width: 600, height: 400 },
        node: nodeBox(140, 120),
        card: { width: 200, height: 100 },
        legend: null,
      }),
    ).toEqual({ left: 150, top: 114, maxHeight: "" });
  });
});

describe("hoverCardLeave", () => {
  it("drops the card at once when the pointer cannot reach it", () => {
    expect(hoverCardLeave({ scrollable: false, pointerOnCard: false })).toBe("hide");
  });

  it("waits out the grace window while the pointer crosses the gap", () => {
    expect(hoverCardLeave({ scrollable: true, pointerOnCard: false })).toBe("wait");
  });

  it("keeps the card while the pointer rests on it", () => {
    expect(hoverCardLeave({ scrollable: true, pointerOnCard: true })).toBe("hold");
  });
});
