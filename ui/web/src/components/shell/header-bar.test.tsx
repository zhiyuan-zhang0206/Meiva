// HeaderBar: label render + sidebar button (mobile) + Memory Graph / Fleet / Insights / Control navigation + children slot.

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { BAR_DIVIDER_CLASS, BAR_HEIGHT_CLASS } from "@/lib/layout/layout";

import { HeaderBar } from "./header-bar";

afterEach(() => {
  cleanup();
});

describe("HeaderBar", () => {
  it("renders label text", () => {
    render(<HeaderBar label="Agent #5 · idle" onOpenSidebar={vi.fn()} sidebarCollapsed={false} onToggleSidebar={vi.fn()} />);
    expect(screen.getByText("Agent #5 · idle")).toBeTruthy();
  });

  it("click sidebar button → onOpenSidebar called", () => {
    const onOpen = vi.fn();
    render(<HeaderBar label="x" onOpenSidebar={onOpen} sidebarCollapsed={false} onToggleSidebar={vi.fn()} />);
    fireEvent.click(screen.getByRole("button", { name: "Open sidebar" }));
    expect(onOpen).toHaveBeenCalled();
  });

  it("floats above the timeline with a blurred translucent background", () => {
    // User ruling 2026-08-06: the header bar is a floating layer over the
    // full-bleed timeline surface (absolute + backdrop blur), not a sticky
    // row in the content column.
    const { container } = render(<HeaderBar label="x" onOpenSidebar={vi.fn()} sidebarCollapsed={false} onToggleSidebar={vi.fn()} />);
    const header = container.firstChild as HTMLElement;
    expect([...header.classList]).toContain("absolute");
    expect([...header.classList]).not.toContain("relative");
    expect(header.className).toContain("top-0");
    expect(header.className).toContain("z-20");
    expect(header.className).toContain("bg-background/80");
    expect(header.className).toContain("backdrop-blur-md");
  });

  it("puts the shared title height and inset divider on the outer header", () => {
    const { container } = render(<HeaderBar label="x" onOpenSidebar={vi.fn()} sidebarCollapsed={false} onToggleSidebar={vi.fn()} />);
    const header = container.querySelector("header")!;
    const inner = container.querySelector("header > div")!;

    expect(header.className).toContain(BAR_HEIGHT_CLASS);
    for (const dividerClass of BAR_DIVIDER_CLASS.split(" ")) {
      expect(header.className).toContain(dividerClass);
    }
    expect(header.className).not.toContain("border-b");
    expect(inner.className).not.toContain("border-b");
  });

  it("maxWidthCss centers the inner content with the timeline column", () => {
    const { container } = render(
      <HeaderBar label="x" onOpenSidebar={vi.fn()} sidebarCollapsed={false} onToggleSidebar={vi.fn()} maxWidthCss="min(40vw, 1280px)" />,
    );
    const inner = container.querySelector("header > div");
    expect((inner as HTMLElement | null)?.style.maxWidth).toBe("min(40vw, 1280px)");
    expect(inner?.className).toContain("mx-auto");
    // w-full: on narrow viewports (no maxWidthCss) the inner bar spans
    // the pane like every other bar instead of shrink-wrapping its
    // content (Task #881).
    expect(inner?.className).toContain("w-full");
  });

  it("children slot renders custom content on the right", () => {
    render(
      <HeaderBar label="x" onOpenSidebar={vi.fn()} sidebarCollapsed={false} onToggleSidebar={vi.fn()}>
        <span data-testid="header-extra">extra</span>
      </HeaderBar>,
    );
    expect(screen.getByTestId("header-extra")).toBeTruthy();
  });

  it("carries the sidebar toggle only while the sidebar is collapsed", () => {
    const onToggle = vi.fn();
    const { rerender } = render(
      <HeaderBar label="x" onOpenSidebar={vi.fn()} sidebarCollapsed={false} onToggleSidebar={onToggle} />,
    );
    expect(screen.queryByTestId("sidebar-toggle")).toBeNull();
    rerender(<HeaderBar label="x" onOpenSidebar={vi.fn()} sidebarCollapsed onToggleSidebar={onToggle} />);
    const button = screen.getByTestId("sidebar-toggle");
    expect(button.getAttribute("aria-label")).toBe("Show sidebar");
    fireEvent.click(button);
    expect(onToggle).toHaveBeenCalledTimes(1);
  });

  it("pins the restore button to the bar's edge so it does not ride with the centred column", () => {
    // meiva:note
    // The conversation column is capped and centred; an in-flow button sat at
    // x=320 of a 1440 viewport (user 2026-10-20), so the button is pinned to the
    // bar and the label keeps a matching inset.
    // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
    render(
      <HeaderBar
        label="x"
        onOpenSidebar={vi.fn()}
        sidebarCollapsed
        onToggleSidebar={vi.fn()}
        maxWidthCss="792px"
      />,
    );
    const button = screen.getByTestId("sidebar-toggle");
    expect(button.className.split(" ")).toContain("absolute");
    expect(button.className.split(" ")).toContain("left-2");
    const label = screen.getByText("x");
    expect(label.parentElement?.className).toContain("md:pl-8");
  });

  it("collapsed sidebar toggle carries the unresolved-alert count and names it", () => {
    // meiva:note
    // User 2026-10-21: the icon row leaves with the rail, so the one standing signal
    // rides the restore button instead of disappearing with it.
    // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
    render(
      <HeaderBar label="x" onOpenSidebar={vi.fn()} sidebarCollapsed onToggleSidebar={vi.fn()} alertCount={4} />,
    );
    expect(screen.getByTestId("sidebar-toggle-alert-count").textContent).toBe("4");
    expect(screen.getByTestId("sidebar-toggle").getAttribute("aria-label")).toContain("4 unresolved alerts");
  });

  it("collapsed sidebar toggle stays quiet with no unresolved alerts", () => {
    render(<HeaderBar label="x" onOpenSidebar={vi.fn()} sidebarCollapsed onToggleSidebar={vi.fn()} />);
    expect(screen.queryByTestId("sidebar-toggle-alert-count")).toBeNull();
    expect(screen.getByTestId("sidebar-toggle").getAttribute("aria-label")).not.toContain("unresolved");
  });

  it("mobile menu button carries the unresolved-alert count too", () => {
    // meiva:note
    // The drawer is the phone's sidebar: closed, it hides the same icon row the
    // desktop rail hides, so the count rides the menu button that opens it.
    // meiva:link ui/web/src/docs/frontend-components/home-shell-chrome/home-shell-chrome.meiva.okf.md
    render(<HeaderBar label="x" onOpenSidebar={vi.fn()} sidebarCollapsed={false} onToggleSidebar={vi.fn()} alertCount={2} />);
    expect(screen.getByTestId("mobile-sidebar-alert-count").textContent).toBe("2");
    expect(screen.getByRole("button", { name: /Open sidebar/ }).getAttribute("aria-label")).toContain("2 unresolved alerts");
  });

  it("mobile menu button stays quiet with no unresolved alerts", () => {
    render(<HeaderBar label="x" onOpenSidebar={vi.fn()} sidebarCollapsed={false} onToggleSidebar={vi.fn()} />);
    expect(screen.queryByTestId("mobile-sidebar-alert-count")).toBeNull();
  });
});
