import { act } from "@testing-library/react";
import { beforeEach, describe, expect, it } from "vitest";

import { useTimelineStore } from "./timeline-store";

describe("timeline store activeTurn anchor lifecycle", () => {
  beforeEach(() => {
    useTimelineStore.getState().switchThread(1, [], false);
  });

  it("tracks turn_started anchor, snapshot transitions, and clears on llm_done/settleTurn", () => {
    const startedIso = "2026-11-19T07:00:00.000Z";
    const expectedMs = Date.parse(startedIso);

    act(() => {
      useTimelineStore.getState().processSseEvent(
        {
          agent_id: 1,
          role: "turn_started",
          turn_id: "t-1",
          started_at: startedIso,
        },
        expectedMs + 100,
      );
    });

    expect(useTimelineStore.getState().turnActive).toBe(true);
    expect(useTimelineStore.getState().activeTurnId).toBe("t-1");
    expect(useTimelineStore.getState().activeTurnStartedAt).toBe(expectedMs);

    act(() => {
      useTimelineStore.getState().processSseEvent(
        {
          agent_id: 1,
          role: "timeline_snapshot",
          items: [
            {
              item_id: "h10.0",
              kind: "agent_code",
              source: null,
              payload: "ls",
              created_at: null,
              inbound_id: null,
              show_timestamp: false,
              turn_id: "t-1",
              turn_duration_ms: 4500,
            },
          ],
          msg_count: 20,
          active_turn_id: null,
          active_turn_started_at: null,
        },
        expectedMs + 4500,
      );
    });
    expect(useTimelineStore.getState().activeTurnId).toBeNull();
    expect(useTimelineStore.getState().activeTurnStartedAt).toBe(0);
    expect(useTimelineStore.getState().items[0].turn_duration_ms).toBe(4500);

    const coldIso = "2026-11-19T08:00:00.000Z";
    act(() => {
      useTimelineStore.getState().reloadSnapshot(
        [],
        0,
        false,
        { id: "t-2", startedAt: coldIso },
      );
    });
    expect(useTimelineStore.getState().activeTurnId).toBe("t-2");
    expect(useTimelineStore.getState().activeTurnStartedAt).toBe(Date.parse(coldIso));

    act(() => {
      useTimelineStore.getState().settleTurn(Date.parse(coldIso) + 5000);
    });
    expect(useTimelineStore.getState().turnActive).toBe(false);
    expect(useTimelineStore.getState().activeTurnId).toBeNull();
    expect(useTimelineStore.getState().activeTurnStartedAt).toBe(0);
  });
});
