import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { InlineError } from "./inline-error";

afterEach(() => {
  cleanup();
});

describe("InlineError", () => {
  it("renders error message with role='alert' and text-destructive tone by default", () => {
    render(<InlineError>Failed to load cluster status</InlineError>);
    const alert = screen.getByRole("alert");
    expect(alert.textContent).toContain("Failed to load cluster status");
    expect(alert.className).toContain("text-destructive");
  });

  it("renders with role='status' when specified", () => {
    render(<InlineError role="status">Status unavailable</InlineError>);
    expect(screen.queryByRole("alert")).toBeNull();
    const status = screen.getByRole("status");
    expect(status.textContent).toContain("Status unavailable");
  });

  it("renders secondary detail text when provided", () => {
    render(
      <InlineError detail="HTTP 503 Service Unavailable">
        Network failure
      </InlineError>,
    );
    expect(screen.getByText("Network failure")).toBeTruthy();
    expect(screen.getByText("HTTP 503 Service Unavailable")).toBeTruthy();
  });

  it("renders retry button when onRetry is passed and fires callback on click", () => {
    const onRetry = vi.fn();
    render(
      <InlineError onRetry={onRetry} retryLabel="Try again" retryTestId="my-retry">
        Action failed
      </InlineError>,
    );
    const button = screen.getByTestId("my-retry");
    expect(button.textContent).toBe("Try again");
    fireEvent.click(button);
    expect(onRetry).toHaveBeenCalledOnce();
  });

  it("disables retry button when retryDisabled is true", () => {
    const onRetry = vi.fn();
    render(
      <InlineError onRetry={onRetry} retryDisabled>
        Loading...
      </InlineError>,
    );
    const button = screen.getByRole("button", { name: "Retry" });
    expect(button.hasAttribute("disabled")).toBe(true);
  });

  it("renders surface='floating' with popover background and shadow", () => {
    render(
      <InlineError surface="floating" data-testid="floating-error">
        Connection lost
      </InlineError>,
    );
    const el = screen.getByTestId("floating-error");
    expect(el.className).toContain("bg-popover");
    expect(el.className).toContain("shadow-md");
    expect(el.className).toContain("border-destructive/40");
  });
});
