import { describe, expect, it } from "vitest";

import {
  hoverCardBridge,
  hoverCardLeave,
  placeHoverCard,
  type HoverCardNodeBox,
} from "../force-graph-geometry";

// meiva:note
// The hover card's geometry and pointer decisions, stated as values: where the card goes, where the
// safe-zone bridge spans the gap, and what a pointer leaving the node or card does next.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md

// meiva:note
// A node's page box (left edge, top edge, square side) as the placement takes it: the right edge
// anchors the beside placements, the left edge the flip.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
function nodeBox(left: number, top: number): HoverCardNodeBox {
  return { x: left + 36, y: top, flipX: left, h: 36 };
}

describe("placeHoverCard", () => {
  it("sits 6px beside a node with its top aligned when there is room to the right", () => {
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 600, height: 400 },
        node: nodeBox(100, 100),
        card: { width: 200, height: 100 },
        legend: null,
      }),
    ).toEqual({ left: 142, top: 100, maxHeight: "", side: "right" });
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
    ).toEqual({ left: 44, top: 100, maxHeight: "", side: "left" });
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
    ).toEqual({ left: 48, top: 74, maxHeight: "", side: "above" });
  });

  it("caps the height on the roomier side when a narrow canvas fits the card neither way", () => {
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 320, height: 390 },
        node: nodeBox(130, 180),
        card: { width: 200, height: 300 },
        legend: null,
      }),
    ).toEqual({ left: 48, top: 4, maxHeight: "170px", side: "above" });
  });

  it("slides a tall beside card upward within the canvas column instead of clipping at the floor", () => {
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 900, height: 900 },
        node: nodeBox(100, 500),
        card: { width: 320, height: 509 },
        legend: null,
      }),
    ).toEqual({ left: 142, top: 387, maxHeight: "", side: "right" });
  });

  it("caps a beside card to the full canvas column when taller than the canvas", () => {
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 900, height: 900 },
        node: nodeBox(100, 400),
        card: { width: 320, height: 1100 },
        legend: null,
      }),
    ).toEqual({ left: 142, top: 4, maxHeight: "892px", side: "right" });
  });

  it("lifts a beside card above the legend while keeping it beside the node", () => {
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 900, height: 900 },
        node: nodeBox(600, 450),
        card: { width: 320, height: 300 },
        legend: { left: 560, top: 700, width: 200, height: 30 },
      }),
    ).toEqual({ left: 274, top: 394, maxHeight: "", side: "left" });
  });

  it("leaves the canvas floor alone when the card misses the legend's column", () => {
    expect(
      placeHoverCard({
        canvas: { left: 0, top: 0, width: 900, height: 900 },
        node: nodeBox(100, 500),
        card: { width: 320, height: 509 },
        legend: { left: 560, top: 700, width: 200, height: 30 },
      }),
    ).toEqual({ left: 142, top: 387, maxHeight: "", side: "right" });
  });

  it("measures the node in the canvas's own frame", () => {
    expect(
      placeHoverCard({
        canvas: { left: 40, top: 20, width: 600, height: 400 },
        node: nodeBox(140, 120),
        card: { width: 200, height: 100 },
        legend: null,
      }),
    ).toEqual({ left: 142, top: 100, maxHeight: "", side: "right" });
  });
});

describe("hoverCardBridge", () => {
  it("spans the 6px gap on the node-facing edge of a right-placed card", () => {
    const canvas = { left: 0, top: 0, width: 600, height: 400 };
    const node = nodeBox(100, 100);
    const card = { width: 200, height: 120 };
    const placement = placeHoverCard({ canvas, node, card, legend: null });
    expect(hoverCardBridge({ canvas, node, card, placement })).toEqual({
      left: "-6px",
      right: "",
      top: "0px",
      bottom: "0px",
      width: "6px",
      height: "",
    });
  });

  it("spans the 6px gap on the right edge when flipped to the left of the node", () => {
    const canvas = { left: 0, top: 0, width: 300, height: 400 };
    const node = nodeBox(250, 100);
    const card = { width: 200, height: 120 };
    const placement = placeHoverCard({ canvas, node, card, legend: null });
    expect(hoverCardBridge({ canvas, node, card, placement })).toEqual({
      left: "",
      right: "-6px",
      top: "0px",
      bottom: "0px",
      width: "6px",
      height: "",
    });
  });

  it("spans the vertical gap when placed above a node on a narrow canvas", () => {
    const canvas = { left: 0, top: 0, width: 320, height: 390 };
    const node = nodeBox(130, 180);
    const card = { width: 200, height: 100 };
    const placement = placeHoverCard({ canvas, node, card, legend: null });
    expect(hoverCardBridge({ canvas, node, card, placement })).toEqual({
      left: "0px",
      right: "0px",
      top: "",
      bottom: "-6px",
      width: "",
      height: "6px",
    });
  });
});

describe("hoverCardLeave", () => {
  it("holds immediately when the pointer crosses onto the partner node or card", () => {
    expect(hoverCardLeave({ pointerOnPartner: true })).toBe("hold");
  });

  it("waits out the grace window when the pointer leaves into the gap or canvas", () => {
    expect(hoverCardLeave({ pointerOnPartner: false })).toBe("wait");
  });
});
