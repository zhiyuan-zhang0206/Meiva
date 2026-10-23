// meiva:note
// ConnectionNotice render tests — the live-update overlay banner. It renders whatever
// the store says; the "is this drop worth announcing" delay lives in
// app-connection-banner, whose own test covers the timing.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";

import { useStore } from "@/lib/state/store";

import { ConnectionNotice } from "./connection-notice";

beforeEach(() => {
  useStore.setState({ connState: "open" });
});

afterEach(() => {
  cleanup();
  useStore.setState({ connState: "open" });
});

describe("ConnectionNotice", () => {
  it("renders nothing when connState='open'", () => {
    const { container } = render(<ConnectionNotice />);
    expect(container.firstChild).toBeNull();
  });

  it("shows amber 'reconnecting' when connState='reconnecting'", () => {
    useStore.setState({ connState: "reconnecting" });
    render(<ConnectionNotice />);
    const banner = screen.getByRole("status");
    expect(banner.textContent).toContain("Live updates reconnecting");
    expect(banner.className).toContain("amber");
  });

  it("shows destructive 'connection lost' when connState='closed'", () => {
    useStore.setState({ connState: "closed" });
    render(<ConnectionNotice />);
    const banner = screen.getByRole("status");
    expect(banner.textContent).toContain("Live updates disconnected");
    expect(banner.textContent).toContain("refresh");
    expect(banner.className).toContain("destructive");
  });

  it("floats over the conversation instead of taking a row of it", () => {
    useStore.setState({ connState: "closed" });
    const { container } = render(<ConnectionNotice />);
    const overlay = container.firstElementChild as HTMLElement;
    expect(overlay.className).toContain("absolute");
  });

  it("has aria-live='polite' for screen reader notification", () => {
    useStore.setState({ connState: "closed" });
    render(<ConnectionNotice />);
    expect(screen.getByRole("status").getAttribute("aria-live")).toBe("polite");
  });
});
