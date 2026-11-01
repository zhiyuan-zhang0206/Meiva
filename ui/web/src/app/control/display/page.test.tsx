// Display settings page tests — basic render verification.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";
import DisplaySettingsPage from "./page";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});

beforeEach(() => {
  window.innerWidth = 1024;
});

function renderPage() {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  const view = renderWithBrowser(
    <QueryClientProvider client={qc}>
      <DisplaySettingsPage />
    </QueryClientProvider>,
  );
  return { ...view, queryClient: qc };
}

// Explicit cleanup: globals are off (vitest.config.mts), so RTL's auto-cleanup
// never registers and the DOM would accumulate one page per test — the
// multiple-elements failures in the slider tests exposed that. Other frontend
// test files (page.test.tsx, composer.test.tsx) follow the same pattern.
afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  transport.getSettings.mockResolvedValue({ settings: [] });
  transport.getModels.mockResolvedValue({ providers: {}, models: {}, default: "" });
  transport.getUiContributions.mockResolvedValue({ themes: [], nav: [], stats: [] });
  transport.getNotificationCategories.mockResolvedValue({ categories: [] });
});

async function replySuggestionsToggle(): Promise<HTMLButtonElement> {
  const label = await screen.findByText("Reply suggestions");
  const toggle = label.parentElement?.parentElement?.parentElement?.querySelector<HTMLButtonElement>(
    '[role="switch"]',
  );
  if (!toggle) throw new Error("reply suggestions toggle not found");
  return toggle;
}

describe("DisplaySettingsPage", () => {
  it("renders the default when a stored value has the wrong type", async () => {
    // meiva:note
    // The settings table crosses the wire as JSON and `mergeUserSettings` copies a row over
    // the defaults without checking its type, so a row written by an older or broken client
    // can hold anything. Each read validates and falls back to the value the page shows
    // before the first load, instead of handing the control a value it cannot render.
    transport.getSettings.mockResolvedValue({
      settings: [
        { key: "display.time_mode", value: 42, updated_at: "2026-09-24T00:00:00Z" },
        { key: "display.language", value: null, updated_at: "2026-09-24T00:00:00Z" },
        { key: "display.show_agent_status", value: "yes", updated_at: "2026-09-24T00:00:00Z" },
        { key: "models.hidden", value: "not-a-list", updated_at: "2026-09-24T00:00:00Z" },
      ],
    });
    renderPage();

    const lastActive = await screen.findByLabelText<HTMLInputElement>("Last active");
    expect(lastActive.checked).toBe(true);
    expect(screen.getByLabelText<HTMLInputElement>("English").checked).toBe(true);
    const statusSwitch = await screen.findByRole("switch", { name: "Show agent status dot" });
    expect(statusSwitch.getAttribute("aria-checked")).toBe("false");
    expect(screen.getByText("Timeline max width")).toBeTruthy();
  });

  it("persists the clock format under display.clock_format", async () => {
    transport.getSettings
      .mockResolvedValueOnce({ settings: [] })
      .mockResolvedValue({
        settings: [
          { key: "display.clock_format", value: "12h", updated_at: "2026-10-19T00:00:00Z" },
        ],
      });
    const { queryClient } = renderPage();
    const twelveHour = await screen.findByLabelText<HTMLInputElement>("12-hour");
    expect(twelveHour.checked).toBe(false);
    expect(screen.getByLabelText<HTMLInputElement>("24-hour").checked).toBe(true);

    fireEvent.click(twelveHour);
    await waitFor(() => expect(transport.putSetting).toHaveBeenCalledWith("display.clock_format", "12h"));
    await waitFor(() =>
      expect(
        queryClient.getQueryData<Record<string, unknown>>(SETTINGS_QUERY_KEY)?.[
          "display.clock_format"
        ],
      ).toBe("12h"),
    );
  });

  it("renders all remaining setting labels and omits retired rows", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Show agent status dot")).toBeTruthy();
    });

    expect(screen.queryByText("Show machine name")).toBeNull();
    expect(screen.queryByText("Render thinking as markdown")).toBeNull();
    expect(screen.queryByText("Show activity line")).toBeNull();
    expect(screen.queryByText("Show timezone")).toBeNull();
    expect(screen.queryByText("Timeline details")).toBeNull();
    expect(screen.queryByText("Compact history")).toBeNull();
    expect(screen.queryByText("Timeline colors")).toBeNull();
    expect(screen.queryByText("Context window in the composer")).toBeNull();
    expect(screen.queryByText("Usage bar width")).toBeNull();
    expect(screen.getByText("Show terminated agents")).toBeTruthy();
    expect(screen.getByText("Clock format")).toBeTruthy();
    expect(screen.getByText("Time display mode")).toBeTruthy();
    expect(screen.getByText("Date format")).toBeTruthy();
    expect(screen.getByText("Awaiting-reply notification")).toBeTruthy();
    expect(screen.getByText("Confirm before terminate")).toBeTruthy();
    expect(screen.getByText("Confirm before restart")).toBeTruthy();
    expect(screen.getByText("Confirm before force kill")).toBeTruthy();
  });

  it("renders one switch per delivered category and writes the whole map", async () => {
    transport.getSettings.mockResolvedValue({
      settings: [{ key: "notifications.categories", value: {}, updated_at: "2026-10-18T00:00:00Z" }],
    });
    transport.getNotificationCategories.mockResolvedValue({
      categories: [
        {
          id: "core.needs_input",
          label: "Agent is waiting on your answer",
          description: "An agent posted a notice that needs a response.",
          importance: "high",
          enabled: false,
        },
      ],
    });
    transport.putSetting.mockResolvedValue({
      key: "notifications.categories", value: {}, updated_at: "2026-10-18T00:00:00Z",
    });
    renderPage();

    const label = await screen.findByText("Agent is waiting on your answer");
    const toggle = label.parentElement?.parentElement?.parentElement?.querySelector<HTMLButtonElement>(
      '[role="switch"]',
    );
    if (!toggle) throw new Error("notification category toggle not found");
    expect(toggle.getAttribute("aria-checked")).toBe("false");

    fireEvent.click(toggle);
    await waitFor(() =>
      expect(transport.putSetting).toHaveBeenCalledWith("notifications.categories", {
        "core.needs_input": true,
      }),
    );
  });

  it("reply suggestions are on by default and the toggle persists the opt-out", async () => {
    transport.putSetting.mockResolvedValue({
      key: "behavior.replySuggestions", value: false, updated_at: "2026-10-11T00:00:00Z",
    });
    renderPage();

    const toggle = await replySuggestionsToggle();
    expect(toggle.getAttribute("aria-checked")).toBe("true");

    fireEvent.click(toggle);
    await waitFor(() => expect(transport.putSetting).toHaveBeenCalledWith("behavior.replySuggestions", false));
  });

  it("shows reply suggestions off when the stored value is false", async () => {
    transport.getSettings.mockResolvedValue({
      settings: [{ key: "behavior.replySuggestions", value: false, updated_at: "2026-10-11T00:00:00Z" }],
    });
    renderPage();

    const toggle = await replySuggestionsToggle();
    expect(toggle.getAttribute("aria-checked")).toBe("false");
  });

  it("treats a malformed show-terminated setting as opt-out", async () => {
    transport.getSettings.mockResolvedValue({
      settings: [
        {
          key: "display.show_terminated",
          value: "true",
          updated_at: "2026-08-27T00:00:00Z",
        },
      ],
    });
    renderPage();

    const label = await screen.findByText("Show terminated agents");
    const row = label.parentElement?.parentElement?.parentElement;
    const toggle = row?.querySelector<HTMLButtonElement>('[role="switch"]');
    expect(toggle?.getAttribute("aria-checked")).toBe("false");
  });

  it("renders the Timeline max width slider with the default pixel width", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Timeline max width")).toBeTruthy();
    });
    const slider = screen.getByLabelText<HTMLInputElement>("Timeline max width");
    expect(slider.type).toBe("range");
    expect(slider.value).toBe("800");
    expect(screen.getByText("800px")).toBeTruthy();
  });

  it("narrow viewport → timeline max-width slider is disabled", async () => {
    window.innerWidth = 375;
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText("Timeline max width")).toBeTruthy();
    });
    const slider = screen.getByLabelText<HTMLInputElement>("Timeline max width");
    expect(slider.disabled).toBe(true);
    fireEvent.change(slider, { target: { value: "960" } });
    expect(transport.putSetting).not.toHaveBeenCalled();
  });

  it("desktop viewport → timeline max-width slider is enabled", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText("Timeline max width")).toBeTruthy();
    });
    const slider = screen.getByLabelText<HTMLInputElement>("Timeline max width");
    expect(slider.disabled).toBe(false);
  });

  it("persists a slider change to display.timeline_width_px (debounced)", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByLabelText("Timeline max width")).toBeTruthy();
    });
    const slider = screen.getByLabelText<HTMLInputElement>("Timeline max width");
    fireEvent.change(slider, { target: { value: "960" } });
    expect(slider.value).toBe("960");
    await waitFor(() => {
      expect(transport.putSetting).toHaveBeenCalledWith("display.timeline_width_px", 960);
    });
  });
});
