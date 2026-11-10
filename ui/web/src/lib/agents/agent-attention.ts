import type { AgentRow } from "../contracts/types";

// meiva:note
// The one indicator a sidebar row and its hover card show for an agent that needs the reader.
// A pending question outranks everything and shows the question mark. Without one, an open
// reply request or an unread delivery shows the same orange dot, and the two are told apart
// only by name. The reader's own open, visible, at-the-latest conversation cannot be unread:
// `reading` hides that agent's unread dot but never a reply request.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.md
export type AgentAttention = "question" | "awaiting" | "unread";

export function agentAttention(
  agent: Pick<AgentRow, "has_pending_question" | "awaiting_response_count" | "unread_user_message_count">,
  reading: boolean,
): AgentAttention | null {
  if (agent.has_pending_question) return "question";
  if (agent.awaiting_response_count > 0) return "awaiting";
  if (agent.unread_user_message_count > 0 && !reading) return "unread";
  return null;
}
