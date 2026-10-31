import { cleanup, screen } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { fakeBrowser } from "@/lib/browser/testing";

import {
  RUN_TIMELINE_SPLIT_LAYOUT_ID,
  RunTimelineWorkspace,
  SIDE_PANEL_MAX,
  SIDE_PANEL_MIN,
} from "./run-timeline-workspace";

beforeEach(() => {
  localStorage.clear();
  window.innerWidth = 1440;
});

afterEach(() => {
  cleanup();
  window.innerWidth = 1024;
  vi.restoreAllMocks();
});

describe("RunTimelineWorkspace", () => {
  it("splits the page with a keyboard-reachable, labelled separator on a wide viewport", async () => {
    renderWithBrowser(<RunTimelineWorkspace main={<p>main</p>} side={<p>side</p>} />);

    const handle = await screen.findByRole("separator", { name: "Resize the details panel" });
    expect(handle.getAttribute("aria-orientation")).toBe("vertical");
    expect(handle.getAttribute("tabindex")).toBe("0");
    expect(screen.getByTestId("run-timeline-main").textContent).toBe("main");
    expect(screen.getByTestId("run-timeline-reader").textContent).toBe("side");
  });

  it("bounds the side panel in pixels", () => {
    expect(SIDE_PANEL_MIN).toBe("300px");
    expect(SIDE_PANEL_MAX).toBe("760px");
    expect(RUN_TIMELINE_SPLIT_LAYOUT_ID).toBe("meiva.run-timeline.split");
  });

  it("stacks the details under the timeline on a narrow viewport, with no separator", async () => {
    window.innerWidth = 800;
    renderWithBrowser(<RunTimelineWorkspace main={<p>main</p>} side={<p>side</p>} />);

    expect(await screen.findByTestId("run-timeline-reader")).toBeTruthy();
    expect(screen.queryByRole("separator")).toBeNull();
  });

  it("still renders when the page refuses local storage", async () => {
    // meiva:note
    // A private window answers `unavailable` to every read and write; the split ratios fall back
    // to this page view's memory instead of being lost with the render.
    const page = fakeBrowser();
    page.refuseStorage(true);
    renderWithBrowser(<RunTimelineWorkspace main={<p>main</p>} side={<p>side</p>} />, {
      browser: page,
    });

    expect(await screen.findByRole("separator")).toBeTruthy();
  });
});
