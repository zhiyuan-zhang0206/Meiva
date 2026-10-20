// meiva:note
// ConnectionNotice render tests — the inline timeline banner. It verifies the three
// connection states: 'open' renders nothing, 'reconnecting' shows the amber line and
// 'closed' the destructive line that tells the reader to refresh.
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

  it("has aria-live='polite' for screen reader notification", () => {
    storeConnState = "closed";
    render(<ConnectionNotice />);
    expect(screen.getByRole("status").getAttribute("aria-live")).toBe("polite");
  });
});
