import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import DisplaySettingsPage from "./page";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
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

afterEach(cleanup);

beforeEach(() => {
  vi.clearAllMocks();
  transport.getSettings.mockResolvedValue({ settings: [] });
  transport.getModels.mockResolvedValue({ providers: {}, models: {}, default: "" });
  transport.getUiContributions.mockResolvedValue({ themes: [], nav: [], stats: [] });
  transport.getNotificationCategories.mockResolvedValue({ categories: [] });
});

describe("DisplaySettingsPage models, themes, language, and colors", () => {
  it("groups the model picker toggles by provider with a header per group", async () => {
    transport.getModels.mockResolvedValue({
      providers: {
        deepseek: ["deepseek-v4-pro"],
        claude: ["claude-sonnet-5", "claude-haiku-4-5-20251001"],
      },
      models: {},
      default: "deepseek-v4-pro",
    });
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("DeepSeek V4 Pro")).toBeTruthy();
    });
    expect(screen.getByText("DeepSeek")).toBeTruthy();
    expect(screen.getByText("Claude")).toBeTruthy();
    expect(screen.getByText("Claude Sonnet 5")).toBeTruthy();
    expect(screen.getByText("Claude Haiku 4.5 20251001")).toBeTruthy();
  });

  it("writes the provider switch to models.hidden_providers and the model switch to models.hidden", async () => {
    transport.getModels.mockResolvedValue({
      providers: { claude: ["claude-opus-5-5", "claude-sonnet-5-5"] },
      models: {},
      default: "claude-opus-5-5",
    });
    renderPage();

    fireEvent.click(await screen.findByRole("switch", { name: "Claude" }));
    await waitFor(() => {
      expect(transport.putSetting).toHaveBeenCalledWith("models.hidden_providers", ["claude"]);
    });

    fireEvent.click(screen.getByRole("switch", { name: "Claude Opus 5.5" }));
    await waitFor(() => {
      expect(transport.putSetting).toHaveBeenCalledWith("models.hidden", ["claude-opus-5-5"]);
    });
  });

  it("drops a superseded model instead of showing a switch that changes nothing", async () => {
    transport.getModels.mockResolvedValue({
      providers: { deepseek: ["deepseek-v4-pro", "deepseek-flash"] },
      models: {
        "deepseek-v4-pro": {
          provider: "deepseek",
          context_window: 128_000,
          harnesses: ["meiva"],
          superseded_by: "deepseek-flash",
        },
        "deepseek-flash": {
          provider: "deepseek",
          context_window: 128_000,
          harnesses: ["meiva"],
          superseded_by: null,
        },
      },
      default: "deepseek-flash",
    });
    renderPage();

    await screen.findByLabelText<HTMLButtonElement>("DeepSeek Flash");
    expect(screen.queryByLabelText("DeepSeek V4 Pro")).toBeNull();
    expect(screen.queryByText(/superseded by/)).toBeNull();
    expect(screen.getByLabelText<HTMLButtonElement>("DeepSeek Flash").disabled).toBe(false);
  });

  it("has no Theme section when no plugin contributes a skin", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Show agent status dot")).toBeTruthy();
    });
    expect(screen.queryByText("Skin")).toBeNull();
  });

  it("lists contributed skins and persists the chosen pack", async () => {
    transport.getUiContributions.mockResolvedValue({
      nav: [],
      stats: [],
      themes: [
        { plugin: "skins", name: "solarized", tokens: { "--background": "#fdf6e3" } },
        { plugin: "other", name: "solarized", tokens: { "--background": "#111111" } },
      ],
    });
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Skin")).toBeTruthy();
    });
    const mine = screen.getByLabelText("solarized (skins) — no dark variant") as unknown as HTMLInputElement;
    expect(screen.getByLabelText("solarized (other) — no dark variant")).toBeTruthy();
    expect((screen.getByLabelText("Default (Meiva)") as unknown as HTMLInputElement).checked).toBe(
      true,
    );

    fireEvent.click(mine);
    await waitFor(() => {
      expect(transport.putSetting).toHaveBeenCalledWith("display.theme_pack", "skins/solarized");
    });
  });

  it("marks only the packs that pin both light and dark", async () => {
    transport.getUiContributions.mockResolvedValue({
      nav: [],
      stats: [],
      themes: [
        { plugin: "skins", name: "flat", tokens: { "--background": "#fdf6e3" } },
        {
          plugin: "skins",
          name: "dusk",
          tokens: { "--background": "#fdf6e3" },
          dark_tokens: { "--background": "#002b36" },
        },
      ],
    });
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Skin")).toBeTruthy();
    });
    expect(screen.getByLabelText("flat (skins) — no dark variant")).toBeTruthy();
    expect(screen.getByLabelText("dusk (skins)")).toBeTruthy();
  });

  it("clears the pack back to null when Default is chosen", async () => {
    transport.getSettings.mockResolvedValue({
      settings: [
        { key: "display.theme_pack", value: "skins/solarized", updated_at: "2026-01-01T00:00:00Z" },
      ],
    });
    transport.getUiContributions.mockResolvedValue({
      nav: [],
      stats: [],
      themes: [{ plugin: "skins", name: "solarized", tokens: { "--background": "#fdf6e3" } }],
    });
    renderPage();

    await waitFor(() => {
      expect(screen.getByText("Skin")).toBeTruthy();
    });
    expect(
      (screen.getByLabelText("solarized (skins) — no dark variant") as unknown as HTMLInputElement).checked,
    ).toBe(true);

    fireEvent.click(screen.getByLabelText("Default (Meiva)"));
    await waitFor(() => {
      expect(transport.putSetting).toHaveBeenCalledWith("display.theme_pack", null);
    });
  });

  it("renders the Language row and persists a locale choice", async () => {
    renderPage();

    await waitFor(() => {
      expect(screen.getAllByText("Language").length).toBeGreaterThan(0);
    });
    const english = screen.getByLabelText("English") as unknown as HTMLInputElement;
    const chinese = screen.getByLabelText("\u4e2d\u6587") as unknown as HTMLInputElement;
    expect(english.checked).toBe(true);
    expect(chinese.checked).toBe(false);

    fireEvent.click(chinese);
    await waitFor(() => {
      expect(transport.putSetting).toHaveBeenCalledWith("display.language", "zh");
    });
  });
});
