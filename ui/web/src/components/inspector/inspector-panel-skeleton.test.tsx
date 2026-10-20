import { render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  InspectorPanelSkeleton,
  LiveSectionsSkeleton,
  SectionSkeleton,
  WindowedSectionsSkeleton,
} from "./inspector-panel-skeleton";

const isLargeMock = vi.fn<() => boolean>(() => true);
vi.mock("@/lib/layout/breakpoint", () => ({
  useBreakpoint: () => ({
    tier: isLargeMock() ? "xl" : "xs",
    isNarrow: !isLargeMock(),
    isLarge: isLargeMock(),
  }),
}));

describe("InspectorPanelSkeleton", () => {
  beforeEach(() => {
    isLargeMock.mockReturnValue(true);
  });

  it("mirrors the real panel's sections and draws no header on desktop", () => {
    const { container } = render(<InspectorPanelSkeleton />);

    const aside = screen.getByTestId("inspector-panel-skeleton");
    expect(aside).toBeTruthy();
    expect(screen.queryByText("Inspector")).toBeNull();
    expect(screen.getByLabelText("Page loading")).toBeTruthy();
    expect(screen.getByLabelText("Context loading")).toBeTruthy();
    expect(screen.getByLabelText("Persistent shells loading")).toBeTruthy();
    expect(screen.getByLabelText("Liveness loading")).toBeTruthy();
    expect(screen.getByLabelText("Configuration overlay loading")).toBeTruthy();
    expect(screen.getByLabelText("Activity loading")).toBeTruthy();
    expect(container.querySelector(".animate-pulse")).toBeTruthy();
    expect(aside.querySelector('[data-slot="scroll-area"] [data-slot="scroll-area-viewport"]')).toBeTruthy();
  });

  it("renders full-screen overlay container on mobile", () => {
    isLargeMock.mockReturnValue(false);
    render(<InspectorPanelSkeleton />);

    const aside = screen.getByTestId("inspector-panel-skeleton");
    expect(aside.parentElement?.className.split(" ")).toEqual(
      expect.arrayContaining(["fixed", "inset-0", "z-50", "flex"]),
    );
    expect(document.querySelector('div[aria-hidden="true"]')).toBeTruthy();
  });
});

describe("Individual Skeletons", () => {
  it("SectionSkeleton renders the given title in aria-label and row count", () => {
    render(<SectionSkeleton title="Test Section" rows={3} />);
    const section = screen.getByLabelText("Test Section loading");
    expect(section).toBeTruthy();
    const rows = section.querySelectorAll(".grid > div");
    expect(rows.length).toBe(3);
  });

  it("LiveSectionsSkeleton renders shells, liveness, config overlay and context", () => {
    render(<LiveSectionsSkeleton />);
    expect(screen.getByLabelText("Persistent shells loading")).toBeTruthy();
    expect(screen.getByLabelText("Liveness loading")).toBeTruthy();
    expect(screen.getByLabelText("Configuration overlay loading")).toBeTruthy();
    expect(screen.getByLabelText("Context loading")).toBeTruthy();
  });

  it("WindowedSectionsSkeleton renders the merged cost and activity section", () => {
    render(<WindowedSectionsSkeleton />);
    expect(screen.getByLabelText("Activity loading")).toBeTruthy();
  });
});
