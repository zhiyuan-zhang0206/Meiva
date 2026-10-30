// meiva:note
// VisualViewportHeightSync is the iOS half of the on-screen keyboard contract (the Android half is
// the `interactiveWidget` viewport export in app/layout.tsx). The heights a keyboard, a toolbar or
// a pinch produces are answers the browser port gives (`fakeBrowser().answerViewport`), so no case
// installs a stub on the global; the decision itself is `pinnedShellHeight` in
// lib/browser/viewport.ts and is asserted directly as well.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import { cleanup } from "@testing-library/react";
import { afterEach, expect, it } from "vitest";

import { fakeBrowser, type FakeBrowser } from "@/lib/browser/testing";
import { KEYBOARD_MIN_VISUAL_VIEWPORT_DELTA_PX, pinnedShellHeight } from "@/lib/browser/viewport";
import { renderWithBrowser } from "@/test-support/render";

import { VisualViewportHeightSync } from "./visual-viewport-height-sync";

const LAYOUT_HEIGHT = 800;

// meiva:note
// The pin lives in one inline style on <html>; a failed render would otherwise leak it into the
// next case, exactly as a real unmount must clear it.
afterEach(() => {
  cleanup();
  document.documentElement.style.height = "";
});

function page(visual: { height: number; scale?: number } | null): FakeBrowser {
  return fakeBrowser({
    viewport: {
      layoutHeight: LAYOUT_HEIGHT,
      visual: visual === null ? null : { height: visual.height, scale: visual.scale ?? 1 },
    },
  });
}

it("pins the shell to the visual viewport while the keyboard is up", () => {
  const browser = page({ height: LAYOUT_HEIGHT - 320 });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });

  expect(document.documentElement.style.height).toBe("480px");
  // meiva:note
  // The keyboard grows, or the user resizes: one answer, one follow.
  browser.answerViewport({ layoutHeight: LAYOUT_HEIGHT, visual: { height: 300, scale: 1 } });
  expect(document.documentElement.style.height).toBe("300px");
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
});

it("restores the layout height when the keyboard closes", () => {
  const browser = page({ height: LAYOUT_HEIGHT - 320 });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });

  browser.answerViewport({ layoutHeight: LAYOUT_HEIGHT, visual: { height: LAYOUT_HEIGHT, scale: 1 } });
  expect(document.documentElement.style.height).toBe("");
});

it("clears the pin on unmount", () => {
  const { unmount } = renderWithBrowser(<VisualViewportHeightSync />, { browser: page({ height: LAYOUT_HEIGHT - 320 }) });
  expect(document.documentElement.style.height).toBe("480px");

  unmount();
  expect(document.documentElement.style.height).toBe("");
});

it("does nothing when the browser has no visual viewport", () => {
  const browser = fakeBrowser({ viewport: { layoutHeight: LAYOUT_HEIGHT, visual: null } });
  renderWithBrowser(<VisualViewportHeightSync />, { browser });

  expect(document.documentElement.style.height).toBe("");
  // meiva:note
  // A page with no visual viewport has nothing to follow, and the shell says so by staying put.
  browser.answerViewport({ layoutHeight: 200, visual: null });
  expect(document.documentElement.style.height).toBe("");
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
