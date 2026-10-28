// meiva:note
// CopyButton (icon-only, code/command-output blocks) — locks the click behavior itself, not just
// that the button renders. The copy goes through the browser port, so the assertion is what the
// component asked the page to do; the two delivery paths behind the port (the async clipboard API
// and the execCommand fallback for plain-HTTP LAN) are the system implementation's own test, in
// lib/browser/system-browser.test.ts.
// meiva:link ui/web/src/docs/frontend-state/browser-port.meiva.okf.md

import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import { fakeBrowser } from "@/lib/browser/testing";
import { renderWithBrowser } from "@/test-support/render";
import { CopyButton } from "./copy-button";

afterEach(cleanup);

describe("CopyButton rendering", () => {
  it("default label — renders with 'Copy code' aria-label", () => {
    renderWithBrowser(<CopyButton text="print(1)" />);
    expect(screen.getByRole("button", { name: "Copy code" })).toBeTruthy();
  });

  it("custom label — aria-label reflects it", () => {
    renderWithBrowser(<CopyButton text="stdout" label="command output" />);
    expect(screen.getByRole("button", { name: "Copy command output" })).toBeTruthy();
  });

  it("default (streaming=false) — positioned at bottom-right", () => {
    renderWithBrowser(<CopyButton text="done" />);
    const btn = screen.getByRole("button", { name: "Copy code" });
    expect(btn.className).toContain("bottom-1.5");
    expect(btn.className).not.toContain("top-1.5");
  });

  it("streaming=true — positioned at top-right so the button stays accessible while tail scrolls", () => {
    renderWithBrowser(<CopyButton text="streaming" streaming />);
    const btn = screen.getByRole("button", { name: "Copy code" });
    expect(btn.className).toContain("top-1.5");
    expect(btn.className).not.toContain("bottom-1.5");
  });
});

describe("CopyButton click behavior", () => {
  it("click → asks the page to copy the given text", async () => {
    const { browser } = renderWithBrowser(<CopyButton text="hello world" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(browser.actions.copied).toEqual(["hello world"]));
  });

  it("click → aria-label flips to 'Copied' as feedback", async () => {
    renderWithBrowser(<CopyButton text="hello world" />);
    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(screen.getByRole("button", { name: "Copied" })).toBeTruthy());
  });

  it("a copy the page refused leaves the button as it was", async () => {
    const browser = fakeBrowser();
    browser.clipboardAccepts(false);
    renderWithBrowser(<CopyButton text="hello world" />, { browser });
    fireEvent.click(screen.getByRole("button", { name: "Copy code" }));
    await waitFor(() => expect(browser.actions.copied).toEqual([]));
    expect(screen.getByRole("button", { name: "Copy code" })).toBeTruthy();
  });
});
