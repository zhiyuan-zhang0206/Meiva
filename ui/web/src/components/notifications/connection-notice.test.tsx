// meiva:note
// ConnectionNotice render tests — the live-update overlay banner. It renders whatever
// the store says; the "is this drop worth announcing" delay lives in
// app-connection-banner, whose own test covers the timing.
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

let storeConnState = "open" as string;

vi.mock("@/lib/state/store", () => ({
  useStore: (selector: (s: { connState: string }) => unknown) =>
    selector({ connState: storeConnState }),
}));

import { ConnectionNotice } from "./connection-notice";

beforeEach(() => {
  storeConnState = "open";
});

afterEach(cleanup);

describe("ConnectionNotice", () => {
  it("renders nothing when connState='open'", () => {
    const { container } = render(<ConnectionNotice />);
    expect(container.firstChild).toBeNull();
  });

  it("shows amber 'reconnecting' when connState='reconnecting'", () => {
    storeConnState = "reconnecting";
    render(<ConnectionNotice />);
    const banner = screen.getByRole("status");
    expect(banner.textContent).toContain("Live updates reconnecting");
    expect(banner.className).toContain("amber");
  });

  it("shows destructive 'connection lost' when connState='closed'", () => {
    storeConnState = "closed";
    render(<ConnectionNotice />);
    const banner = screen.getByRole("status");
    expect(banner.textContent).toContain("Live updates disconnected");
    expect(banner.textContent).toContain("refresh");
    expect(banner.className).toContain("destructive");
  });

  it("floats over the conversation instead of taking a row of it", () => {
    storeConnState = "closed";
    const { container } = render(<ConnectionNotice />);
    const overlay = container.firstElementChild as HTMLElement;
    expect(overlay.className).toContain("absolute");
  });

  it("has aria-live='polite' for screen reader notification", () => {
    storeConnState = "closed";
    render(<ConnectionNotice />);
    expect(screen.getByRole("status").getAttribute("aria-live")).toBe("polite");
  });
});
