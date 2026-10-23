// ContextMeter tests — the composer's context readout: a gauge whose fill
// crosses amber past the soft (wind-down) threshold and red past the hard
// (force-compact) ceiling, with tick marks at both, plus a numeric summary.
//
// happy-dom + RTL — vitest globals=false; explicit cleanup.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { USER_SETTING_DEFAULTS } from "@/lib/contracts/types";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";
import { api } from "@/lib/transport/api";

import { ContextButton, type ContextButtonProps } from "./context-breakdown";
import { ContextBreakdownCard } from "./context-breakdown-body";
import { ContextMeter, ContextMeterGhost, resolveContextMeterWidth } from "./context-meter";

vi.mock("@/lib/transport/api", () => ({
  api: {
    getContextBreakdown: vi.fn(),
    getRunTimelineContext: vi.fn(),
    getSettings: vi.fn(() => Promise.resolve({ settings: [] })),
  },
}));

function apiError(status: number, message: string): Error {
  return Object.assign(new Error(message), { status });
}

const getContextBreakdown = vi.mocked(api.getContextBreakdown);
const getRunTimelineContext = vi.mocked(api.getRunTimelineContext);

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
});

describe("ContextMeter", () => {
  const full = {
    contextTokens: 300_000,
    maxContextTokens: 1_000_000,
    softCompactTokens: 600_000,
    hardCompactTokens: 800_000,
  };

  it("renders the gauge + numeric summary with soft/hard values", () => {
    render(<ContextMeter {...full} />);
    const text = screen.getByTestId("context-meter").textContent;
    expect(text).toBe("Context: 300.0k/1.00M · soft 600.0k · hard 800.0k");
    // Fill width = occupancy / window; marks at soft/hard percentages.
    expect(screen.getByTestId("context-meter-fill").getAttribute("style")).toContain("width: 30%");
    expect(screen.getByTestId("context-meter-soft-mark").getAttribute("style")).toContain(
      "left: 60%",
    );
    expect(screen.getByTestId("context-meter-hard-mark").getAttribute("style")).toContain(
      "left: 80%",
    );
  });

  it("merges textClassName into the numeric text (callers hide it at narrow widths)", () => {
    render(<ContextMeter {...full} textClassName="hidden sm:inline" />);
    const text = screen.getByTestId("context-meter").querySelector("span.truncate");
    expect(text?.className).toContain("hidden");
    expect(text?.className).toContain("sm:inline");
  });

  it("fill is neutral below the soft threshold", () => {
    render(<ContextMeter {...full} contextTokens={300_000} />);
    expect(screen.getByTestId("context-meter-fill").className).toContain("bg-muted-foreground/60");
  });

  it("fill turns amber in the wind-down band (soft < occupancy <= hard)", () => {
    render(<ContextMeter {...full} contextTokens={700_000} />);
    expect(screen.getByTestId("context-meter-fill").className).toContain("bg-amber-500");
  });

  it("fill turns red past the hard ceiling and clamps the width", () => {
    render(<ContextMeter {...full} contextTokens={1_200_000} />);
    const fill = screen.getByTestId("context-meter-fill");
    expect(fill.className).toContain("bg-destructive");
    expect(fill.getAttribute("style")).toContain("width: 100%"); // clamped, not 120%
  });

  it("omits the gauge + thresholds when the model window is unknown (max 0)", () => {
    render(
      <ContextMeter
        contextTokens={5000}
        maxContextTokens={0}
        softCompactTokens={0}
        hardCompactTokens={0}
      />,
    );
    expect(screen.queryByTestId("context-meter-fill")).toBeNull();
    expect(screen.getByTestId("context-meter").textContent).toBe("Context: 5.0k tokens");
  });

  it("shows the gauge but no marks when the window is known but thresholds are not", () => {
    render(
      <ContextMeter
        contextTokens={26_000}
        maxContextTokens={1_000_000}
        softCompactTokens={0}
        hardCompactTokens={0}
      />,
    );
    expect(screen.getByTestId("context-meter-fill")).toBeTruthy();
    expect(screen.queryByTestId("context-meter-soft-mark")).toBeNull();
    expect(screen.getByTestId("context-meter").textContent).toBe("Context: 26.0k/1.00M tokens");
  });

  it("defaults the gauge track to the compact width when no barWidthClassName is given", () => {
    render(<ContextMeter {...full} />);
    expect(screen.getByRole("img").className).toContain("w-16");
  });

  it("applies a caller-supplied bar width class (the display.context_meter_width tiers)", () => {
    render(<ContextMeter {...full} barWidthClassName="w-48" />);
    const gauge = screen.getByRole("img");
    expect(gauge.className).toContain("w-48");
    expect(gauge.className).not.toContain("w-16");
  });
});

describe("resolveContextMeterWidth", () => {
  it("passes through each valid tier", () => {
    expect(resolveContextMeterWidth("compact")).toBe("compact");
    expect(resolveContextMeterWidth("comfortable")).toBe("comfortable");
    expect(resolveContextMeterWidth("wide")).toBe("wide");
  });

  it("falls back to comfortable for an unset or stale/invalid setting value", () => {
    expect(resolveContextMeterWidth(undefined)).toBe("comfortable");
    expect(resolveContextMeterWidth(null)).toBe("comfortable");
    expect(resolveContextMeterWidth("huge")).toBe("comfortable");
  });
});

describe("ContextMeterGhost", () => {
  it("renders a decorative pulsing track in the meter's geometry", () => {
    wrap(<ContextMeterGhost />);
    const ghost = screen.getByTestId("context-meter-ghost");
    expect(ghost.getAttribute("aria-hidden")).toBe("true");
    const track = ghost.querySelector("span");
    expect(track?.className).toContain("h-1.5");
    expect(track?.className).toContain("animate-pulse");
    // Unset setting -> the comfortable tier (w-32), the meter default.
    expect(track?.className).toContain("w-32");
  });

  it("follows the display.context_meter_width setting", () => {
    wrap(<ContextMeterGhost />, { "display.context_meter_width": "wide" });
    const track = screen.getByTestId("context-meter-ghost").querySelector("span");
    expect(track?.className).toContain("w-48");
  });
});

const breakdown = {
  total_input_tokens: 1000,
  estimated: false,
  exact_fraction: 1,
  max_input_tokens: 1_000_000,
  soft_compact_tokens: 600_000,
  hard_compact_tokens: 800_000,
  sections: [
    { name: "(preamble)", tokens: 100, estimated: true },
    { name: "Tools", tokens: 400, estimated: true },
  ],
  categories: [
    { kind: "system_prompt", tokens: 400, estimated: false, exact_fraction: 1 },
    { kind: "output", tokens: 300, estimated: false, exact_fraction: 1 },
    { kind: "reasoning", tokens: 100, estimated: false, exact_fraction: 1 },
    { kind: "agent_messages", tokens: 150, estimated: false, exact_fraction: 1 },
    { kind: "automation", tokens: 50, estimated: false, exact_fraction: 1 },
  ],
};

const emptyBreakdown = {
  total_input_tokens: 0,
  estimated: false,
  exact_fraction: 1,
  max_input_tokens: 0,
  soft_compact_tokens: 0,
  hard_compact_tokens: 0,
  sections: [],
  categories: [],
};

const meterProps = {
  contextTokens: 26_000,
  maxContextTokens: 1_000_000,
  softCompactTokens: 600_000,
  hardCompactTokens: 800_000,
};

function Harness(props: Omit<ContextButtonProps, "open" | "onOpenChange">) {
  const [open, setOpen] = useState(false);
  return <ContextButton {...props} open={open} onOpenChange={setOpen} />;
}

function wrap(ui: React.ReactElement, settings: Record<string, unknown> = {}) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS, ...settings });
  return render(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

describe("ContextBreakdownCard (P4-3)", () => {
  it("thresholds in the panel come from the fetched response, not the live meter props", async () => {
    getContextBreakdown.mockResolvedValue({
      ...breakdown,
      max_input_tokens: 2_000_000,
      soft_compact_tokens: 1_200_000,
      hard_compact_tokens: 1_600_000,
    });
    wrap(<Harness agentId={7} {...meterProps} />);
    expect(screen.getByTestId("context-meter-button").textContent).toContain("Context: 26.0k/1.00M");
    fireEvent.click(screen.getByTestId("context-meter-button"));
    await screen.findByTestId("context-breakdown-total");
    const lines = [...screen.getByTestId("context-breakdown-total").querySelectorAll("span.block")];
    expect(lines.map((l) => l.textContent)).toEqual([
      "1.0k / 2.00M tokens",
      "soft 1.20M · hard 1.60M",
    ]);
  });

  it("renders the breakdown inline on mount — no trigger, no popover", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<ContextBreakdownCard agentId={7} />);
    const card = await screen.findByTestId("context-breakdown-card");
    expect(card.querySelector("h2")?.textContent).toBe("Context breakdown");
    expect(getContextBreakdown).toHaveBeenCalledWith(7);
    expect(screen.queryByTestId("context-meter-button")).toBeNull();
    expect(screen.queryByTestId("context-breakdown-panel")).toBeNull();
    await screen.findByTestId("context-breakdown-categories");
    const total = screen.getByTestId("context-breakdown-total");
    expect([...total.querySelectorAll("span.block")].map((l) => l.textContent)).toEqual([
      "1.0k / 1.00M tokens",
      "soft 600.0k · hard 800.0k",
    ]);
    expect(screen.getByText("Agent messages")).toBeTruthy();
    expect(screen.getByText("System notes")).toBeTruthy();
    fireEvent.click(screen.getByTestId("context-breakdown-sections-toggle"));
    expect(screen.getByText("(preamble)")).toBeTruthy();
  });

  it("shows the explicit empty state inline", async () => {
    getContextBreakdown.mockResolvedValue(emptyBreakdown);
    wrap(<ContextBreakdownCard agentId={3} />);
    const empty = await screen.findByTestId("context-breakdown-empty");
    expect(empty.textContent).toContain("No context recorded yet");
    expect(screen.queryByTestId("context-breakdown-categories")).toBeNull();
  });

  it("classifies a failure inline with Retry", async () => {
    getContextBreakdown.mockRejectedValueOnce(new Error("HTTP 500: Internal Server Error"));
    getContextBreakdown.mockResolvedValueOnce(breakdown);
    wrap(<ContextBreakdownCard agentId={9} />);
    const err = await screen.findByTestId("context-breakdown-error");
    expect(err.textContent).toContain("the server returned an error");
    fireEvent.click(screen.getByTestId("context-breakdown-retry"));
    await screen.findByTestId("context-breakdown-categories");
  });

  it("a point with no LLM request (404) is the neutral empty state, not a red failure", async () => {
    getRunTimelineContext.mockRejectedValue(apiError(404, "the agent has made no LLM request"));
    wrap(<ContextBreakdownCard agentId={9} at={5} />);
    await screen.findByTestId("context-breakdown-empty");
    expect(screen.queryByTestId("context-breakdown-error")).toBeNull();
  });

  it("an error answer carrying a JSON detail is classified as a server error, not unreachable", async () => {
    getRunTimelineContext.mockRejectedValue(apiError(503, "cluster is paused"));
    wrap(<ContextBreakdownCard agentId={9} at={5} />);
    const err = await screen.findByTestId("context-breakdown-error");
    expect(err.textContent).toContain("the server returned an error");
    expect(err.textContent).toContain("cluster is paused");
  });

  it("carries the scope subtitle on the card (agent-scoped, not the window)", async () => {
    getContextBreakdown.mockResolvedValue(breakdown);
    wrap(<ContextBreakdownCard agentId={7} />);
    const subtitle = await screen.findByTestId("context-breakdown-subtitle");
    expect(subtitle.textContent).toBe("The composition of the current context");
  });

  it("prefixes the total, a category and a section with ~ only when flagged", async () => {
    getContextBreakdown.mockResolvedValue({
      ...breakdown,
      estimated: true,
      exact_fraction: 0.5,
      categories: [
        { kind: "system_prompt", tokens: 400, estimated: true, exact_fraction: 0 },
        { kind: "output", tokens: 600, estimated: false, exact_fraction: 1 },
      ],
    });
    wrap(<ContextBreakdownCard agentId={7} />);
    const total = await screen.findByTestId("context-breakdown-total");
    expect(total.textContent).toMatch(/^~[\d.,]+k? .*tokens/);
    expect(screen.getByText(/^~400/)).toBeTruthy();
    expect(screen.getByText(/^600 · /).textContent).not.toContain("~");
    fireEvent.click(screen.getByTestId("context-breakdown-sections-toggle"));
    expect(screen.getAllByText(/^~\d/).length).toBeGreaterThanOrEqual(2);
    const sections = screen.getByTestId("context-breakdown-sections");
    expect(sections.textContent).toContain("~100");
    expect(sections.textContent).toContain("~400");
  });

  it("no prefix when the total and categories are exact", async () => {
    getContextBreakdown.mockResolvedValue({ ...breakdown, sections: [] });
    wrap(<ContextBreakdownCard agentId={7} />);
    const total = await screen.findByTestId("context-breakdown-total");
    expect(total.textContent).not.toContain("~");
    expect(screen.getByTestId("context-breakdown-categories").textContent).not.toContain("~");
  });
});
