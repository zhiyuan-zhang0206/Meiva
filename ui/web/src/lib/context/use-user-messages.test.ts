import { expect, it } from "vitest";
import type { BackendTimelineItem, UserMessage } from "../contracts/types";
import { applySystemEvent } from "../fold/timeline";
import { withUserMessages } from "./use-user-messages";

const publication: UserMessage = {
  id: 1, agent_id: 7, mode: "prefix", source_key: "assistant:message-1:0.0",
  content: "Ready", suggestions: [],
  created_at: "2026-11-06T00:00:00Z",
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

it("recovers SDK publications independently of compacted conversation history", () => {
  const merged = withUserMessages([], [{ ...publication, mode: "sdk", source_key: "sdk:publication-1" }]);
  expect(merged).toHaveLength(1);
  expect(merged[0].payload).toBe("Ready");
});

it.each(["naive", "prefix"] as const)("keeps %s history pagination owned by the timeline", (mode) => {
  const record = { ...publication, mode };
  const recent = applySystemEvent([], { role: "chat_delta", agent_id: 7, item_id: "9.0", content: "Latest", delivery_mode: "naive", source_message_id: "message-2" }, 1);
  expect(withUserMessages(recent, [record]).map((item) => item.item_id)).toEqual(["9.0"]);
  const older = applySystemEvent([], { role: "chat_delta", agent_id: 7, item_id: "2.0", content: "Ready", delivery_mode: "naive", source_message_id: "message-1" }, 1);
  const loaded = withUserMessages([...older, ...recent], [record]);
  expect(loaded.map((item) => item.item_id)).toEqual(["2.0", "9.0"]);
  expect(loaded[0].userDelivery).toEqual(record);
});

it("uses persisted source identity after message indexes change through compaction", () => {
  const streamed = applySystemEvent([], { role: "chat_delta", agent_id: 7, item_id: "2.0", content: "MEIVA_DISPLAY_TO_USER\nReady", delivery_mode: "prefix", source_message_id: "message-1" }, 1);
  expect(withUserMessages(streamed, [publication])).toHaveLength(1);
});

// meiva:note
// An external harness item has no source message id; its record is keyed by the item id the
// projector wrote (harness:h{seq}.0), the id the history API returns unchanged, also when the
// harness items are merged into a checkpoint timeline.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.md
const harnessItem = (itemId: string, payload: string): BackendTimelineItem => ({
  item_id: itemId, kind: "agent_chat", source: null, payload, created_at: "2026-11-10T00:00:00Z",
  inbound_id: null, user_facing: true, show_timestamp: true,
});

it("attaches an external harness record to the item whose id its source key names", () => {
  const record: UserMessage = { ...publication, id: 4, source_key: "harness:h5.0", content: "From the harness" };
  const merged = withUserMessages([harnessItem("h3.0", "other"), harnessItem("h5.0", "From the harness")], [record]);
  expect(merged.map((item) => item.userDelivery?.id)).toEqual([undefined, 4]);
});

it("does not attach a harness record to an item with another id, and renders no row for it", () => {
  const record: UserMessage = { ...publication, id: 4, source_key: "harness:h5.0", content: "Lost", mode: "prefix" };
  const merged = withUserMessages([harnessItem("h6.0", "Lost")], [record]);
  expect(merged).toHaveLength(1);
  expect(merged[0].userDelivery).toBeUndefined();
});

it("keeps harness and native records on their own items in a merged cross-harness timeline", () => {
  const native: BackendTimelineItem = { ...harnessItem("0.1", "native"), source_message_id: "message-1" };
  const nativeRecord: UserMessage = { ...publication, id: 1, source_key: "assistant:message-1:0.1" };
  const harnessRecord: UserMessage = { ...publication, id: 2, source_key: "harness:h9.0", content: "harness" };
  const merged = withUserMessages([native, harnessItem("h9.0", "harness")], [nativeRecord, harnessRecord]);
  expect(merged.map((item) => item.userDelivery?.id)).toEqual([1, 2]);
});
