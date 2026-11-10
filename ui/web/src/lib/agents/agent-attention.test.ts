import { describe, expect, it } from "vitest";

import { agentAttention } from "./agent-attention";

const quiet = { has_pending_question: false, awaiting_response_count: 0, unread_user_message_count: 0 };

describe("agentAttention", () => {
  it("shows nothing for an agent that needs nobody", () => {
    expect(agentAttention(quiet, false)).toBeNull();
  });

  it("maps an unread delivery to the unread dot", () => {
    expect(agentAttention({ ...quiet, unread_user_message_count: 3 }, false)).toBe("unread");
  });

  it("maps an open reply request to the awaiting dot", () => {
    expect(agentAttention({ ...quiet, awaiting_response_count: 1 }, false)).toBe("awaiting");
  });

  it("lets a pending question outrank both the reply request and the unread count", () => {
    expect(
      agentAttention({ has_pending_question: true, awaiting_response_count: 2, unread_user_message_count: 5 }, false),
    ).toBe("question");
  });

  it("lets a reply request outrank the unread count so only one dot ever shows", () => {
    expect(agentAttention({ ...quiet, awaiting_response_count: 1, unread_user_message_count: 4 }, false)).toBe("awaiting");
  });

  it("hides the unread dot of the conversation being read, and nothing else", () => {
    expect(agentAttention({ ...quiet, unread_user_message_count: 3 }, true)).toBeNull();
    expect(agentAttention({ ...quiet, awaiting_response_count: 1, unread_user_message_count: 3 }, true)).toBe("awaiting");
    expect(agentAttention({ ...quiet, has_pending_question: true, unread_user_message_count: 3 }, true)).toBe("question");
  });
});
