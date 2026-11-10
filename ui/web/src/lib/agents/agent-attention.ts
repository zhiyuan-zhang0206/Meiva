import type { AgentRow } from "../contracts/types";

// meiva:note
// The one indicator a sidebar row and its hover card show for an agent that needs the reader, and
// there are only two: a pending question shows the question mark and outranks everything; otherwise
// deliveries past the reader's seen position show the orange dot. The conversation the reader has
// open in a visible page counts as seen, so `reading` hides its dot at once. Open reply requests
// and the notice counters do not take part.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.md
export type AgentAttention = "question" | "unread";

export function agentAttention(
  agent: Pick<AgentRow, "has_pending_question" | "has_unread_user_messages">,
  reading: boolean,
): AgentAttention | null {
  if (agent.has_pending_question) return "question";
  if (agent.has_unread_user_messages && !reading) return "unread";
  return null;
}
