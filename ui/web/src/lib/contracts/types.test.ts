// Exhaustive wire fixtures force optional schema additions into projection tests.
// Future optional extensions survive remapping without another allowlist.
import { expect, it } from "vitest";
import { projectAgentStatus } from "./types";
import type { WireAgentCard, WireAgentRow } from "./types";

const availability: NonNullable<WireAgentRow["availability"]> = {
  reason: "launch_unreachable", observed_at: "2026-09-01T00:00:00Z",
  evidence_at: "2026-09-01T00:00:00Z",
};
const detail: Required<WireAgentRow> = {
  last_launch_attempt_id: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  agent_id: 7, spawner: "user", harness: "meiva", fork_source_agent_id: null,
  fork_source_checkpoint_id: "detail-only-checkpoint", status: "idling",
  spawned_at: "2026-09-01T00:00:00Z",
  last_active_at: "2026-09-01T00:00:00Z", last_inbound_at: "2026-09-01T00:00:00Z",
  label: "detail row", machine: "test-host", supports_vision: true,
  liveness_state: "unknown", last_probe_at: "2026-09-01T00:00:00Z",
  availability, observation: null, notices_awaiting_response: [],
  unread_notice_count: 2, heartbeat_paused_until: null,
  unread_user_message_count: 0,
  harness_exit_code: null, harness_exit_at: null,
};
const wireCard: Required<WireAgentCard> = {
  agent_id: 9, spawner: "user", harness: "meiva", fork_source_agent_id: null, status: "running",
  spawned_at: "2026-09-01T00:00:00Z",
  last_active_at: "2026-09-01T00:00:00Z", last_inbound_at: "2026-09-01T00:00:00Z",
  label: "card", machine: "test-host", supports_vision: true, liveness_state: "unknown",
  availability, observation: { runtime_owner: "unknown" }, awaiting_response_count: 3, has_pending_question: false,
  highest_notice_priority: "P2", unread_notice_count: 2, unread_user_message_count: 0, heartbeat_paused_until: null,
  harness_exit_code: null, harness_exit_at: null,
};
function card(overrides: Partial<WireAgentCard>): Required<WireAgentCard> {
  return { ...wireCard, ...overrides };
}

it("keeps every card field and input identity when status is unchanged", () => {
  const input = card({ status: "idling" });
  expect(projectAgentStatus(input)).toBe(input);
});
it("keeps optional detail extensions while dropping detail-only fields and aggregating notices", () => {
  const notices: WireAgentRow["notices_awaiting_response"] = (["P3", "P1", "P2"] as const).map(
    (priority, index) => ({
      id: index, title: `notice ${index}`, content: "selected-detail-only body", priority,
      require_response: true, blocking: false, created_at: "2026-09-01T00:00:00Z",
      question: index === 1 ? { type: "text" } : null,
    }),
  );
  const input: Required<WireAgentRow> & { future_optional?: string } = {
    ...detail, future_optional: "detail-extension", notices_awaiting_response: notices,
  };
  const before = structuredClone(input);
  const projected = projectAgentStatus(input);
  const { notices_awaiting_response: _notices, fork_source_checkpoint_id: _fork, last_probe_at: _probe, ...shared } = input;
  expect(projected).toEqual({
    ...shared, awaiting_response_count: 3, has_pending_question: true, highest_notice_priority: "P1",
  });
  for (const excluded of ["notices_awaiting_response", "fork_source_checkpoint_id", "last_probe_at"]) {
    expect(projected).not.toHaveProperty(excluded);
  }
  expect(input).toEqual(before);
  expect(projected.availability?.reason).toBe("launch_unreachable");
});
it("a detail retains native status and the empty-notice aggregate", () => {
  const projected = projectAgentStatus({ ...detail, status: "running" });
  expect(projected.status).toBe("running");
  expect(projected.awaiting_response_count).toBe(0);
  expect(projected.has_pending_question).toBe(false);
  expect(projected.highest_notice_priority).toBeNull();
});
