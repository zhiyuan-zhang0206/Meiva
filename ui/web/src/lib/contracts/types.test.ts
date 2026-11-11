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
  availability, observation: null, questions_awaiting_response: [],
    heartbeat_paused_until: null,
  has_unread_user_messages: false,
  harness_exit_code: null, harness_exit_at: null,
};
const wireCard: Required<WireAgentCard> = {
  agent_id: 9, spawner: "user", harness: "meiva", fork_source_agent_id: null, status: "running",
  spawned_at: "2026-09-01T00:00:00Z",
  last_active_at: "2026-09-01T00:00:00Z", last_inbound_at: "2026-09-01T00:00:00Z",
  label: "card", machine: "test-host", supports_vision: true, liveness_state: "unknown",
  llm_model: "claude-opus-4-8", reasoning_effort: "high",
  availability, observation: { runtime_owner: "unknown" }, has_pending_question: false,
    has_unread_user_messages: false, heartbeat_paused_until: null,
  harness_exit_code: null, harness_exit_at: null,
};
function card(overrides: Partial<WireAgentCard>): Required<WireAgentCard> {
  return { ...wireCard, ...overrides };
}

it("keeps every card field and input identity when status is unchanged", () => {
  const input = card({ status: "idling" });
  expect(projectAgentStatus(input)).toBe(input);
});
it("keeps optional detail extensions while dropping detail-only fields and deriving the question flag", () => {
  const questions: WireAgentRow["questions_awaiting_response"] = ([0, 1, 2] as const).map(
    (index) => ({
      id: index, title: `question ${index}`, content: "selected-detail-only body",
      created_at: "2026-09-01T00:00:00Z",
      question: { type: "text" },
    }),
  );
  const input: Required<WireAgentRow> & { future_optional?: string } = {
    ...detail, future_optional: "detail-extension", questions_awaiting_response: questions,
  };
  const before = structuredClone(input);
  const projected = projectAgentStatus(input);
  const { questions_awaiting_response: _questions, fork_source_checkpoint_id: _fork, last_probe_at: _probe, ...shared } = input;
  expect(projected).toEqual({
    ...shared, has_pending_question: true,
    llm_model: null, reasoning_effort: null,
  });
  for (const excluded of ["questions_awaiting_response", "fork_source_checkpoint_id", "last_probe_at"]) {
    expect(projected).not.toHaveProperty(excluded);
  }
  expect(input).toEqual(before);
  expect(projected.availability?.reason).toBe("launch_unreachable");
});
it("a detail retains native status and an empty question flag", () => {
  const projected = projectAgentStatus({ ...detail, status: "running" });
  expect(projected.status).toBe("running");
  expect(projected.has_pending_question).toBe(false);
});
