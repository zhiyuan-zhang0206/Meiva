// meiva:note
// ConnectionNotice render tests — the live-update overlay banner.
// Renders nothing during transient blips or reconnecting states;
// surfaces as an un-occluded red floating error banner below HeaderBar (top-[52px] z-30)
// only when connState is "closed" (sustained disconnect >= 15s).
// meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md

import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

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

  it("renders nothing when connState='reconnecting' (transient blip is completely silent)", () => {
    useStore.setState({ connState: "reconnecting" });
    const { container } = render(<ConnectionNotice />);
    expect(container.firstChild).toBeNull();
  });

  it("shows floating destructive error with Retry when connState='closed'", () => {
    const bumpSpy = vi.fn();
    useStore.setState({ connState: "closed", bumpReconnect: bumpSpy });
    render(<ConnectionNotice />);

    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Live updates disconnected");
    expect(alert.className).toContain("text-destructive");
    expect(alert.className).toContain("bg-popover");

    const retryBtn = screen.getByRole("button", { name: "Retry" });
    fireEvent.click(retryBtn);
    expect(bumpSpy).toHaveBeenCalledOnce();
  });

  it("floats over conversation clearing HeaderBar with top-[52px] and z-30", () => {
    useStore.setState({ connState: "closed" });
    const { container } = render(<ConnectionNotice />);
    const overlay = container.firstElementChild as HTMLElement;
    expect(overlay.className).toContain("absolute");
    expect(overlay.className).toContain("top-[52px]");
    expect(overlay.className).toContain("z-30");
  });
});
