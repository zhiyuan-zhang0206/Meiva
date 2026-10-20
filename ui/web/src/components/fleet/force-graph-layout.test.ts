import { describe, expect, it } from "vitest";

import { layoutOptions } from "./force-graph";

// meiva:note
// The threshold is the whole decision: below it a small graph keeps the live
// d3 settle (it animates into place), at or above it the layout runs in manual
// slices so a big graph does not repaint the SVG once per tick.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.meiva.okf.md
describe("layoutOptions", () => {
  it("keeps the live settle below the threshold", () => {
    expect(layoutOptions(199)).toEqual({});
    expect(layoutOptions(0)).toEqual({});
  });

  it("prewarms at the threshold and above", () => {
    expect(layoutOptions(200)).toEqual({ prewarmTicks: 240 });
    expect(layoutOptions(1949)).toEqual({ prewarmTicks: 240 });
  });
});
