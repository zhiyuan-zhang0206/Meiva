import { afterEach, describe, expect, it, vi } from "vitest";
import { cleanup, screen } from "@testing-library/react";
import { render } from "@/lib/transport/tests/wall-clock-render";

import { TurnBlock } from "./run-block";
import type { TurnSummary } from "./model/runs";

afterEach(cleanup);

const baseSummary: TurnSummary = {
  total: 120,
  thinking: 120,
  code: 0,
  output: 0,
  turns: 120,
  systemPrompts: 0,
  compactSummaries: 0,
  memories: 0,
  agentMessages: 0,
  systemNotes: 0,
  settledActiveMs: 6_000,
  priorActiveMs: 0,
  liveSegmentStartMs: 1_700_000_000_000,
  sdkCalls: [],
};

// meiva:note
// An expanded block has one height: its own. Whatever the turn did, the body is
// rendered in the timeline's own flow - no inner scrollport, no cap, no spacer
// window - so a finished block is exactly as tall as it was and a running one
// extends downward from its header (user ruling 2026-11-09).
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.md
describe("TurnBlock expanded body", () => {
  it("renders every row in the timeline's flow, with no inner scrollport and no cap", () => {
    const memberIds = Array.from({ length: 120 }, (_, i) => `${i + 1}.0`);
    const { container } = render(
      <TurnBlock id="1.0" memberIds={memberIds} summary={baseSummary} expanded onToggle={vi.fn()}>
        {memberIds.map((id) => (
          <div key={id} data-testid="turn-row">{`Row ${id}`}</div>
        ))}
      </TurnBlock>,
    );

    expect(screen.getAllByTestId("turn-row")).toHaveLength(120);
    expect(screen.queryByTestId("turn-scroll-viewport")).toBeNull();
    expect(screen.queryByTestId("activity-scroll-viewport")).toBeNull();
    expect(container.querySelector('[data-turn-scroll="true"]')).toBeNull();
    expect(container.querySelector('[data-timeline-spacer="turn-before"]')).toBeNull();
    expect(container.querySelector('[data-timeline-spacer="turn-after"]')).toBeNull();
    expect(container.querySelector('[class*="max-h-[var(--timeline-turn"]')).toBeNull();
  });

  it("mounts the live window's rows in the same flow while the turn runs", () => {
    const memberIds = ["1.0", "1.1"];
    render(
      <TurnBlock id="1.0" memberIds={memberIds} summary={baseSummary} expanded turnActive liveWindow onToggle={vi.fn()}>
        <div data-testid="turn-row">thinking</div>
        <div data-testid="turn-row">output</div>
      </TurnBlock>,
    );
    expect(screen.getAllByTestId("turn-row")).toHaveLength(2);
    expect(screen.queryByTestId("activity-scroll-viewport")).toBeNull();
  });

  it("renders no body while collapsed", () => {
    render(
      <TurnBlock id="1.0" memberIds={["1.0"]} summary={baseSummary} expanded={false} onToggle={vi.fn()}>
        <div data-testid="turn-row">hidden</div>
      </TurnBlock>,
    );
    expect(screen.queryByTestId("turn-row")).toBeNull();
  });
});

describe("TurnBlock missing timing and active-segment display", () => {
  it("shows plain Worked without a warning icon when settledActiveMs is null", () => {
    render(<TurnBlock id="partial" memberIds={["1"]} expanded={false} onToggle={vi.fn()}
      summary={{ ...baseSummary, settledActiveMs: null, priorActiveMs: null, liveSegmentStartMs: null }} />);
    expect(screen.getByTestId("turn-toggle").textContent).toBe("Worked");
    expect(screen.queryByRole("img")).toBeNull();
    expect(screen.getByTestId("turn-toggle").querySelector("[title], [aria-label]")).toBeNull();
  });

  it("keeps a live block with missing priorActiveMs or missing anchor plain Working without a warning icon", () => {
    const { rerender } = render(<TurnBlock id="live" memberIds={["1"]} expanded={false} onToggle={vi.fn()}
      turnActive summary={{ ...baseSummary, settledActiveMs: null, priorActiveMs: 0, liveSegmentStartMs: null }} />);
    expect(screen.getByTestId("turn-toggle").textContent).toBe("Working…");
    expect(screen.queryByRole("img")).toBeNull();

    rerender(<TurnBlock id="live" memberIds={["1"]} expanded={false} onToggle={vi.fn()}
      turnActive lastEventAt={1_700_000_005_000}
      summary={{ ...baseSummary, settledActiveMs: null, priorActiveMs: null, liveSegmentStartMs: 1_700_000_000_000 }} />);
    expect(screen.getByTestId("turn-toggle").textContent).toBe("Working…");
    expect(screen.queryByRole("img")).toBeNull();
  });

  it("holds priorActiveMs when a live block has closed its prior segment and has no live anchor yet", () => {
    render(<TurnBlock id="live-hold" memberIds={["1"]} expanded={false} onToggle={vi.fn()}
      turnActive lastEventAt={0}
      summary={{ ...baseSummary, settledActiveMs: 600_000, priorActiveMs: 600_000, liveSegmentStartMs: null }} />);
    expect(screen.getByTestId("turn-toggle").textContent).toBe("Working for 10m");
  });

  it.each([0, 400, 6000])("formats complete active duration %ims", (activeMs) => {
    render(<TurnBlock id="complete" memberIds={["1"]} expanded={false} onToggle={vi.fn()}
      summary={{ ...baseSummary, settledActiveMs: activeMs }} />);
    expect(screen.getByTestId("turn-toggle").textContent).toBe(activeMs < 1000 ? "Worked for 1s" : "Worked for 6s");
    expect(screen.queryByRole("img")).toBeNull();
  });
});
