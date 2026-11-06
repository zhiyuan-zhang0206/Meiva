import { expect, it } from "vitest";
import type { UserMessage } from "../contracts/types";
import { applySystemEvent } from "../fold/timeline";
import { withUserMessages } from "./use-user-messages";

const publication: UserMessage = {
  id: 1, agent_id: 7, mode: "prefix", source_key: "assistant:message-1:0.0",
  content: "Ready", suggestions: [],
  created_at: "2026-11-06T00:00:00Z", read_at: null,
};

it.each(["naive", "prefix", "sdk"] as const)("keeps %s routing across start, chunks and durable publication", (mode) => {
  const text = mode === "prefix" ? "MEIVA_DISPLAY_TO_USER\nReady" : "Ready";
  const start = applySystemEvent([], { role: "chat_start", agent_id: 7, item_id: "3.0", delivery_mode: mode, source_message_id: "message-1" }, 1);
  const streamed = applySystemEvent(start, { role: "chat_delta", agent_id: 7, item_id: "3.0", content: text, delivery_mode: mode, source_message_id: "message-1" }, 2);
  expect(streamed[0].payload).toBe("Ready");
  expect(streamed[0].user_facing).toBe(mode !== "sdk");
  const records = mode === "sdk" ? [{ ...publication, mode, source_key: "sdk:publication-1" }] : [{ ...publication, mode }];
  const merged = withUserMessages(streamed, records);
  expect(merged.filter((item) => item.user_facing)).toHaveLength(1);
  expect(merged.find((item) => item.userDelivery)?.userDelivery?.id).toBe(1);
});

it("recovers durable messages independently of compacted conversation history", () => {
  const merged = withUserMessages([], [publication]);
  expect(merged).toHaveLength(1);
  expect(merged[0].payload).toBe("Ready");
  expect(merged[0].userDelivery?.read_at).toBeNull();
});

it("uses persisted source identity after message indexes change through compaction", () => {
  const streamed = applySystemEvent([], { role: "chat_delta", agent_id: 7, item_id: "2.0", content: "MEIVA_DISPLAY_TO_USER\nReady", delivery_mode: "prefix", source_message_id: "message-1" }, 1);
  expect(withUserMessages(streamed, [publication])).toHaveLength(1);
});
