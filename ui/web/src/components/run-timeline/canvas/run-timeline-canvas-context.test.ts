// meiva:note
// The canvases' default drawing surface is the page's own: `systemCanvasContext` asks the element
// for its "2d" context. Every surface test hands in a recording factory instead, so this file
// guards the default those tests replace — a default bound to a fake ahead of time would never ask
// the page, and nothing else would notice.
import { describe, expect, it, vi } from "vitest";

import { systemCanvasContext } from "./run-timeline-canvas";

describe("systemCanvasContext", () => {
  it("asks the canvas for its 2d context and returns what the page answers", () => {
    const el = document.createElement("canvas");
    const getContext = vi.fn(() => null);
    Object.defineProperty(el, "getContext", { configurable: true, value: getContext });

    expect(systemCanvasContext(el)).toBeNull();
    expect(getContext).toHaveBeenCalledWith("2d");
  });
});
