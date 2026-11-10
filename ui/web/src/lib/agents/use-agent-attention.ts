"use client";

import type { AgentRow } from "../contracts/types";
import { useStore } from "../state/store";
import { type AgentAttention, agentAttention } from "./agent-attention";

export function useAgentAttention(agent: AgentRow): AgentAttention | null {
  const reading = useStore((state) => state.readingAgentId === agent.agent_id);
  return agentAttention(agent, reading);
}
