
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { providerLabel } from "@/lib/contracts/models";
import type { ModelsResponse } from "@/lib/contracts/types";

import { ModelPickerSection } from "./_model_picker";

const GROUPS: [provider: string, models: string[]][] = [
  ["deepseek", ["deepseek-flash"]],
  ["claude", ["claude-opus-5-5", "claude-sonnet-5-5"]],
];

const MODELS: ModelsResponse["models"] = {
  "deepseek-flash": {
    provider: "deepseek",
    context_window: 128_000,
    pricing: { input: 0.28, cache_read: 0.028, output: 0.42 },
    harnesses: ["meiva"],
  },
  "claude-opus-5-5": {
    provider: "claude",
    context_window: 1_000_000,
    pricing: { input: 2, cache_read: 0.2, output: 10 },
    harnesses: ["meiva"],
  },
  "claude-sonnet-5-5": {
    provider: "claude",
    context_window: 1_000_000,
    pricing: { input: 2, cache_read: 0.2, output: 10 },
    harnesses: ["meiva"],
  },
};

function renderPicker(overrides: Partial<Parameters<typeof ModelPickerSection>[0]> = {}) {
  const props = {
    groups: GROUPS,
    models: MODELS,
    hiddenModels: [] as string[],
    hiddenProviders: [] as string[],
    onToggleModel: vi.fn(),
    onToggleProvider: vi.fn(),
    ...overrides,
  };
  render(<ModelPickerSection {...props} />);
  return props;
}

afterEach(cleanup);

const switchNamed = (name: string) => screen.getByRole("switch", { name });

describe("ModelPickerSection hierarchy", () => {
  it("keeps every model inside the group its provider heading owns", () => {
    renderPicker();
    const section = screen.getByTestId("model-picker");

    for (const [provider, models] of GROUPS) {
      const heading = within(section).getByRole("heading", { name: providerLabel(provider) });
      const group = heading.closest("[data-provider-group]");
      expect(group?.getAttribute("data-provider-group")).toBe(provider);

      for (const model of models) {
        const toggle = within(section).getByRole("switch", { name: model });
        expect(toggle.closest("[data-provider-group]")).toBe(group);
      }
    }

    expect(section.querySelectorAll("[data-provider-group]")).toHaveLength(GROUPS.length);
  });

  it("prints a provider name once instead of in every row", () => {
    renderPicker();
    const text = screen.getByTestId("model-picker").textContent;

    expect(text.split("DeepSeek")).toHaveLength(2);
    expect(text.split("Claude")).toHaveLength(2);
  });

  it("reaches a provider before the models it governs", () => {
    renderPicker();
    const section = screen.getByTestId("model-picker");

    expect(within(section).getAllByRole("switch").map((el) => el.getAttribute("aria-label"))).toEqual([
      "DeepSeek",
      "deepseek-flash",
      "Claude",
      "claude-opus-5-5",
      "claude-sonnet-5-5",
    ]);
    const tabbable = [...section.querySelectorAll("[tabindex]")].filter(
      (el) => Number(el.getAttribute("tabindex")) > 0,
    );
    expect(tabbable).toHaveLength(0);
  });

  it("reports which level was switched", () => {
    const props = renderPicker();

    fireEvent.click(switchNamed("Claude"));
    expect(props.onToggleProvider).toHaveBeenCalledWith("claude", false);

    fireEvent.click(switchNamed("deepseek-flash"));
    expect(props.onToggleModel).toHaveBeenCalledWith("deepseek-flash", false);
    expect(props.onToggleProvider).toHaveBeenCalledTimes(1);
  });

  it("turns a switched-off family readable as off without losing its own state", () => {
    renderPicker({ hiddenProviders: ["claude"], hiddenModels: ["claude-opus-5-5"] });

    const opus = switchNamed("claude-opus-5-5") as HTMLButtonElement;
    const sonnet = switchNamed("claude-sonnet-5-5") as HTMLButtonElement;
    expect(opus.disabled).toBe(true);
    expect(sonnet.disabled).toBe(true);
    expect(switchNamed("Claude").getAttribute("aria-checked")).toBe("false");
  });
});
