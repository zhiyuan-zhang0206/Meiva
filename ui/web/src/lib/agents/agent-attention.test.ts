import { describe, expect, it } from "vitest";

import { agentAttention } from "./agent-attention";

const quiet = { has_pending_question: false, has_unread_user_messages: false };

describe("agentAttention", () => {
  it("shows nothing for an agent that needs nobody", () => {
    expect(agentAttention(quiet, false)).toBeNull();
  });

  it("shows the orange dot for deliveries past the seen position", () => {
    expect(agentAttention({ ...quiet, has_unread_user_messages: true }, false)).toBe("unread");
  });

  it("lets a pending question outrank unread so only one indicator ever shows", () => {
    expect(agentAttention({ has_pending_question: true, has_unread_user_messages: true }, false)).toBe("question");
  });

  it("hides the dot of the conversation being read, never the question", () => {
    expect(agentAttention({ ...quiet, has_unread_user_messages: true }, true)).toBeNull();
    expect(agentAttention({ has_pending_question: true, has_unread_user_messages: true }, true)).toBe("question");
  });
});
