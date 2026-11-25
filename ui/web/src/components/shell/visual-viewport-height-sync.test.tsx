// meiva:note
// VisualViewportHeightSync is the iOS half of the on-screen keyboard contract (the Android half is
// the `interactiveWidget` viewport export in app/layout.tsx). The heights a keyboard, a toolbar or
// a pinch produces are answers the browser port gives (`fakeBrowser().answerViewport`), so no case
// installs a stub on the global; the decision itself is `pinnedShellHeight` in
// lib/browser/viewport.ts and is asserted directly as well.
// meiva:link ui/web/src/docs/frontend-state/browser-port.md

import { cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { fakeBrowser, type FakeBrowser } from "@/lib/browser/testing";
import {
  KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX,
  pinnedShellHeight,
  pinnedShellWidth,
  VIEWPORT_SCALE_EPSILON,
  VIEWPORT_WIDTH_PIN_MIN_DELTA_PX,
} from "@/lib/browser/viewport";
import { renderWithBrowser } from "@/test-support/render";

import { VisualViewportHeightSync } from "./visual-viewport-height-sync";

const LAYOUT_HEIGHT = 800;

// meiva:note
// The pin lives in one inline style on <html>; a failed render would otherwise leak it into the
// next case, exactly as a real unmount must clear it.
afterEach(() => {
  cleanup();
  document.documentElement.style.height = "";
  document.documentElement.style.width = "";
  document.documentElement.style.minWidth = "";
  document.documentElement.style.maxWidth = "";
  document.documentElement.style.overflowX = "";
  document.documentElement.style.removeProperty("--visual-viewport-height");
  document.documentElement.style.removeProperty("--visual-viewport-width");
  document.documentElement.style.removeProperty("--safe-area-bottom");
});

function page(visual: { height: number; scale?: number } | null): FakeBrowser {
  return fakeBrowser({
    viewport: {
      layoutHeight: LAYOUT_HEIGHT,
      visual: visual === null ? null : { height: visual.height, scale: visual.scale ?? 1 },
    },
  });
}

it("pins the shell to the visual viewport and resets root scroll while the keyboard is up", () => {
  const browser = page({ height: LAYOUT_HEIGHT - 320 });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });

  expect(document.documentElement.style.height).toBe("480px");
  expect(document.documentElement.style.getPropertyValue("--visual-viewport-height")).toBe("480px");
  expect(document.documentElement.style.getPropertyValue("--safe-area-bottom")).toBe("0px");
  expect(browser.actions.viewportScrollResets).toBe(1);
  // meiva:note
  // The keyboard grows, or WebKit pans the focused field: one answer, one follow, and the root
  // scroll returns to (0, 0) so the composer is not double-shifted off the top edge.
  browser.answerViewport({ layoutHeight: LAYOUT_HEIGHT, visual: { height: 300, scale: 1 } });
  expect(document.documentElement.style.height).toBe("300px");
  expect(document.documentElement.style.getPropertyValue("--visual-viewport-height")).toBe("300px");
  expect(document.documentElement.style.getPropertyValue("--safe-area-bottom")).toBe("0px");
  expect(browser.actions.viewportScrollResets).toBe(2);
});

it("rounds a fractional visual viewport height", () => {
  renderWithBrowser(<VisualViewportHeightSync />, { browser: page({ height: LAYOUT_HEIGHT - 320.4 }) });

  expect(document.documentElement.style.height).toBe("480px");
});

it("leaves the shell alone when the shortfall is chrome-sized", () => {
  // Exactly at the threshold: a conservative bound, so browser chrome alone
  // (toolbar collapse/expand, tens of px) never shrinks the page.
  renderWithBrowser(<VisualViewportHeightSync />, {
    browser: page({ height: LAYOUT_HEIGHT - KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX }),
  });

  expect(document.documentElement.style.height).toBe("");
});

it("drops the pin while the user pinch-zooms", () => {
  const browser = page({ height: LAYOUT_HEIGHT - 320 });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });
  expect(document.documentElement.style.height).toBe("480px");

  browser.answerViewport({ layoutHeight: LAYOUT_HEIGHT, visual: { height: LAYOUT_HEIGHT - 320, scale: 2 } });
  expect(document.documentElement.style.height).toBe("");
  expect(document.documentElement.style.getPropertyValue("--visual-viewport-height")).toBe("");
  expect(document.documentElement.style.getPropertyValue("--safe-area-bottom")).toBe("");
});

it("restores the layout height and resets root scroll when the keyboard closes", () => {
  const browser = page({ height: LAYOUT_HEIGHT - 320 });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });
  expect(browser.actions.viewportScrollResets).toBe(1);

  browser.answerViewport({ layoutHeight: LAYOUT_HEIGHT, visual: { height: LAYOUT_HEIGHT, scale: 1 } });
  expect(document.documentElement.style.height).toBe("");
  expect(document.documentElement.style.getPropertyValue("--visual-viewport-height")).toBe("");
  expect(document.documentElement.style.getPropertyValue("--safe-area-bottom")).toBe("");
  expect(browser.actions.viewportScrollResets).toBe(2);
});

it("clears the pin on unmount", () => {
  const { unmount } = renderWithBrowser(<VisualViewportHeightSync />, { browser: page({ height: LAYOUT_HEIGHT - 320 }) });
  expect(document.documentElement.style.height).toBe("480px");

  unmount();
  expect(document.documentElement.style.height).toBe("");
  expect(document.documentElement.style.getPropertyValue("--visual-viewport-height")).toBe("");
  expect(document.documentElement.style.getPropertyValue("--safe-area-bottom")).toBe("");
});

it("does nothing when the browser has no visual viewport", () => {
  const browser = fakeBrowser({ viewport: { layoutHeight: LAYOUT_HEIGHT, visual: null } });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });

  expect(document.documentElement.style.height).toBe("");
  expect(browser.actions.viewportScrollResets).toBe(0);
  // meiva:note
  // A page with no visual viewport has nothing to follow, and the shell says so by staying put.
  browser.answerViewport({ layoutHeight: 200, visual: null });
  expect(document.documentElement.style.height).toBe("");
  expect(browser.actions.viewportScrollResets).toBe(0);
});

it("decides the pin from the reading alone", () => {
  const layout = 800;
  expect(pinnedShellHeight({ layoutHeight: layout, visual: { height: 480, scale: 1 } })).toBe(480);
  expect(pinnedShellHeight({ layoutHeight: layout, visual: { height: 479.6, scale: 1 } })).toBe(480);
  expect(
    pinnedShellHeight({ layoutHeight: layout, visual: { height: layout - KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX, scale: 1 } }),
  ).toBeNull();
  expect(pinnedShellHeight({ layoutHeight: layout, visual: { height: 480, scale: 1.5 } })).toBeNull();
  expect(pinnedShellHeight({ layoutHeight: layout, visual: null })).toBeNull();
  // meiva:note
  // A caller can raise the threshold without touching the module's own constant.
  expect(pinnedShellHeight({ layoutHeight: layout, visual: { height: 480, scale: 1 } }, 400)).toBeNull();
});

it("pins the shell width when an unzoomed visual viewport is narrower than a stuck layout viewport (iPad Chrome rotation)", () => {
  const browser = fakeBrowser({
    viewport: {
      layoutHeight: 820,
      layoutWidth: 1180,
      visual: { height: 1180, width: 820, scale: 1 },
    },
  });
  const { unmount } = renderWithBrowser(<VisualViewportHeightSync />, { browser });

  expect(document.documentElement.style.width).toBe("820px");
  expect(document.documentElement.style.minWidth).toBe("820px");
  expect(document.documentElement.style.maxWidth).toBe("820px");
  expect(document.documentElement.style.overflowX).toBe("hidden");
  expect(document.documentElement.style.getPropertyValue("--visual-viewport-width")).toBe("820px");
  expect(browser.actions.viewportScrollResets).toBe(1);

  browser.answerViewport({
    layoutHeight: 820,
    layoutWidth: 1180,
    visual: { height: 820, width: 1180, scale: 1 },
  });
  expect(document.documentElement.style.width).toBe("");
  expect(document.documentElement.style.minWidth).toBe("");
  expect(document.documentElement.style.maxWidth).toBe("");
  expect(document.documentElement.style.overflowX).toBe("");
  expect(document.documentElement.style.getPropertyValue("--visual-viewport-width")).toBe("");
  expect(browser.actions.viewportScrollResets).toBe(2);

  unmount();
});

it("decides the width pin from the reading alone", () => {
  expect(
    pinnedShellWidth({
      layoutHeight: 820,
      layoutWidth: 1180,
      visual: { height: 1180, width: 820, scale: 1 },
    }),
  ).toBe(820);
  expect(
    pinnedShellWidth({
      layoutHeight: 820,
      layoutWidth: 1180,
      visual: { height: 1180, width: 1180 - VIEWPORT_WIDTH_PIN_MIN_DELTA_PX, scale: 1 },
    }),
  ).toBeNull();
  expect(
    pinnedShellWidth({
      layoutHeight: 820,
      layoutWidth: 1180,
      visual: { height: 1180, width: 590, scale: 2 },
    }),
  ).toBeNull();
});

const LANDSCAPE = { width: 1180, height: 820 };
const PORTRAIT = { width: 820, height: 1180 };

function stuck(layout: { width: number; height: number }, visual: { width: number; height: number }, scale = 1) {
  return {
    layoutHeight: layout.height,
    layoutWidth: layout.width,
    visual: { height: visual.height, width: visual.width, scale },
  };
}

function rootWidthPin() {
  const style = document.documentElement.style;
  return [style.width, style.minWidth, style.maxWidth, style.overflowX, style.getPropertyValue("--visual-viewport-width")];
}

it("pins the shell wider when a portrait layout viewport is stuck after rotating to landscape", () => {
  const browser = fakeBrowser({ viewport: stuck(PORTRAIT, LANDSCAPE) });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });

  expect(rootWidthPin()).toEqual(["1180px", "1180px", "1180px", "hidden", "1180px"]);
});

it("keeps the safe area while the height is stuck with the width, and zeroes it for a keyboard alone", () => {
  const browser = fakeBrowser({ viewport: stuck({ width: 820, height: 1180 }, LANDSCAPE) });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });

  expect(document.documentElement.style.height).toBe("820px");
  expect(document.documentElement.style.getPropertyValue("--visual-viewport-height")).toBe("820px");
  expect(document.documentElement.style.getPropertyValue("--safe-area-bottom")).toBe("");
  expect(document.documentElement.style.width).toBe("1180px");

  browser.answerViewport(stuck(LANDSCAPE, { width: 1180, height: 500 }));
  expect(document.documentElement.style.getPropertyValue("--safe-area-bottom")).toBe("0px");
  expect(document.documentElement.style.width).toBe("");
});

it("pins the shell taller when a landscape layout height is stuck after rotating to portrait", () => {
  renderWithBrowser(<VisualViewportHeightSync />, {
    browser: fakeBrowser({ viewport: stuck(LANDSCAPE, PORTRAIT) }),
  });

  expect(document.documentElement.style.height).toBe("1180px");
  expect(document.documentElement.style.width).toBe("820px");
});

it("still pins through WebKit's float jitter around scale 1, and never against a real pinch", () => {
  for (const scale of [0.9999, 1.005, 1 - VIEWPORT_SCALE_EPSILON / 2, 1 + VIEWPORT_SCALE_EPSILON / 2]) {
    const { unmount } = renderWithBrowser(<VisualViewportHeightSync />, {
      browser: fakeBrowser({ viewport: stuck(LANDSCAPE, PORTRAIT, scale) }),
    });
    expect(document.documentElement.style.width).toBe("820px");
    unmount();
  }

  const browser = fakeBrowser({ viewport: stuck(LANDSCAPE, PORTRAIT, 2) });
  const { unmount } = renderWithBrowser(<VisualViewportHeightSync />, { browser });
  expect(rootWidthPin()).toEqual(["", "", "", "", ""]);
  expect(document.documentElement.style.height).toBe("");
  browser.recheckViewport();
  expect(browser.actions.viewportScaleResets).toBe(0);
  unmount();
});

it("asks the browser to reset the scale on every rotation, whatever the reading says", () => {
  const browser = fakeBrowser({ viewport: stuck(LANDSCAPE, LANDSCAPE) });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });
  expect(browser.actions.viewportScaleResets).toBe(0);

  browser.rotateViewport(stuck(LANDSCAPE, PORTRAIT, 0.7));
  expect(browser.actions.viewportScaleResets).toBe(1);
  browser.rotateViewport(stuck(PORTRAIT, LANDSCAPE, 1.44));
  expect(browser.actions.viewportScaleResets).toBe(2);
  expect(browser.actions.viewportScrollResets).toBeGreaterThanOrEqual(3);
});

it("rechecks a settled rotation or a returning tab, and only resets while the width is still stuck", () => {
  const browser = fakeBrowser({ viewport: stuck(LANDSCAPE, LANDSCAPE) });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });

  browser.recheckViewport();
  expect(browser.actions.viewportScaleResets).toBe(0);

  browser.recheckViewport(stuck(LANDSCAPE, PORTRAIT));
  expect(browser.actions.viewportScaleResets).toBe(1);
  expect(document.documentElement.style.width).toBe("820px");

  browser.answerViewport(stuck(LANDSCAPE, PORTRAIT));
  browser.answerViewport(stuck(LANDSCAPE, PORTRAIT));
  expect(browser.actions.viewportScaleResets).toBe(1);

  browser.recheckViewport(stuck(PORTRAIT, PORTRAIT));
  expect(browser.actions.viewportScaleResets).toBe(1);
  expect(rootWidthPin()).toEqual(["", "", "", "", ""]);
});

it("resets again 180ms into a rotation while WebKit still holds the zoom-to-fit scale, and not once it took", () => {
  const browser = fakeBrowser({ viewport: stuck(LANDSCAPE, LANDSCAPE) });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });

  browser.rotateViewport(stuck(LANDSCAPE, PORTRAIT, 0.7));
  expect(browser.actions.viewportScaleResets).toBe(1);
  browser.rotationRecheckViewport(stuck(LANDSCAPE, PORTRAIT, 0.7));
  expect(browser.actions.viewportScaleResets).toBe(2);

  browser.rotateViewport(stuck(PORTRAIT, LANDSCAPE, 1.44));
  expect(browser.actions.viewportScaleResets).toBe(3);
  browser.rotationRecheckViewport(stuck(PORTRAIT, LANDSCAPE, 1.44));
  expect(browser.actions.viewportScaleResets).toBe(4);

  browser.rotationRecheckViewport(stuck(LANDSCAPE, PORTRAIT));
  expect(browser.actions.viewportScaleResets).toBe(5);

  browser.rotationRecheckViewport(stuck(PORTRAIT, PORTRAIT));
  expect(browser.actions.viewportScaleResets).toBe(5);
});

it("never pulls back a pinch on a plain recheck", () => {
  const browser = fakeBrowser({ viewport: stuck(PORTRAIT, PORTRAIT) });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });
  browser.recheckViewport(stuck(PORTRAIT, PORTRAIT, 1.44));
  expect(browser.actions.viewportScaleResets).toBe(0);
});

it("resets a viewport that is already stuck when the shell mounts", () => {
  const browser = fakeBrowser({ viewport: stuck(LANDSCAPE, PORTRAIT) });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });
  expect(browser.actions.viewportScaleResets).toBe(1);
});

it("clears every pin and stops listening on unmount", () => {
  const browser = fakeBrowser({ viewport: stuck(PORTRAIT, LANDSCAPE) });
  const { unmount } = renderWithBrowser(<VisualViewportHeightSync />, { browser });
  expect(rootWidthPin()[0]).toBe("1180px");

  unmount();
  expect(rootWidthPin()).toEqual(["", "", "", "", ""]);
  expect(document.documentElement.style.height).toBe("");
  const resets = browser.actions.viewportScaleResets;
  browser.rotateViewport(stuck(LANDSCAPE, PORTRAIT));
  expect(browser.actions.viewportScaleResets).toBe(resets);
  expect(rootWidthPin()).toEqual(["", "", "", "", ""]);
});

it("decides the width pin in both directions and through float jitter", () => {
  expect(pinnedShellWidth(stuck(LANDSCAPE, PORTRAIT))).toBe(820);
  expect(pinnedShellWidth(stuck(PORTRAIT, LANDSCAPE))).toBe(1180);
  expect(pinnedShellWidth(stuck(PORTRAIT, { width: 820 + VIEWPORT_WIDTH_PIN_MIN_DELTA_PX, height: 1180 }))).toBeNull();
  expect(pinnedShellWidth(stuck({ width: 1280, height: 800 }, { width: 1263, height: 800 }))).toBeNull();
  expect(pinnedShellWidth(stuck(LANDSCAPE, PORTRAIT, 0.9999))).toBe(820);
  expect(pinnedShellWidth(stuck(LANDSCAPE, PORTRAIT, 1.005))).toBe(820);
  expect(pinnedShellWidth(stuck(LANDSCAPE, PORTRAIT, 1.25))).toBeNull();
  expect(pinnedShellWidth(stuck(LANDSCAPE, PORTRAIT, 0.7))).toBeNull();
  expect(pinnedShellWidth({ layoutHeight: 800, visual: { height: 800, scale: 1 } })).toBeNull();
  expect(pinnedShellWidth({ layoutHeight: 800, layoutWidth: 1180, visual: { height: 800, width: 0, scale: 1 } })).toBeNull();
});

it("pins a layout height that is stuck taller or shorter than the visual band, through float jitter", () => {
  expect(pinnedShellHeight({ layoutHeight: 1180, visual: { height: 820, scale: 0.9999 } })).toBe(820);
  expect(pinnedShellHeight({ layoutHeight: 820, visual: { height: 1180, scale: 1.005 } })).toBe(1180);
  expect(pinnedShellHeight({ layoutHeight: 820, visual: { height: 1180, scale: 1.5 } })).toBeNull();
});
