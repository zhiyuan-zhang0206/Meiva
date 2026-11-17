import type { components } from "./types-generated";

// meiva:note
// A reader-selected snapshot with durable provenance and a display locator.
export type Quote = components["schemas"]["Quote"];

export function isQuote(value: unknown): value is Quote {
  if (typeof value !== "object" || value === null) return false;
  return "source_agent_id" in value && typeof value.source_agent_id === "number" &&
    "source_item_id" in value && typeof value.source_item_id === "string" &&
    "author" in value && typeof value.author === "string" &&
    (!("created_at" in value) || value.created_at === null || typeof value.created_at === "string") &&
    "text" in value && typeof value.text === "string" && value.text.trim().length > 0 &&
    Array.from(value.text).length <= 4000 &&
    (!("source_message_id" in value) || value.source_message_id === null || typeof value.source_message_id === "string") &&
    (!("source_inbound_id" in value) || value.source_inbound_id === null || typeof value.source_inbound_id === "number") &&
    (!("source_block_idx" in value) || value.source_block_idx === null || typeof value.source_block_idx === "number");
}
