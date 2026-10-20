"use client";

// One coherent live tree; a selected historical conversation is a separate
// ID-addressed read. Neither selection nor lineage depends on loading history.
import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";
import { api } from "../transport/api";
import { errMsg } from "../contracts/errors";
import { AGENTS_QUERY_KEY, AGENT_DETAIL_QUERY_KEY } from "../fold/agents";
import { REFERENCE_PARAM } from "../context/reference-url";
import { useStore } from "../state/store";
import { useAgentActions } from "./use-agent-actions";
import type { AgentRoster } from "../contracts/types";

export { AGENTS_QUERY_KEY } from "../fold/agents";
export type { PendingAction } from "./use-agent-actions";
const EMPTY_ROSTER: AgentRoster = { agents: [], ancestors: [] };
const ACTIVE_ID_KEY = "meiva.active.agent_id";

// meiva:note
// A browser can refuse storage (private windows, disabled site data). The
// selection still works for the session; it just stops being remembered.
// meiva:link ui/web/src/docs/frontend-state/navigation-history.meiva.okf.md
function readRememberedAgent(): string | null {
  try {
    return localStorage.getItem(ACTIVE_ID_KEY);
  } catch {
    return null;
  }
}

function rememberAgent(id: number | null): void {
  try {
    if (id == null) localStorage.removeItem(ACTIVE_ID_KEY);
    else localStorage.setItem(ACTIVE_ID_KEY, String(id));
  } catch {
    // meiva:link ui/web/src/docs/frontend-state/navigation-history.meiva.okf.md
  }
}

export function useAgentRoster() {
  return useQuery({
    queryKey: AGENTS_QUERY_KEY,
    queryFn: ({ signal }) => api.getAgentRoster(signal),
    staleTime: Infinity,
  });
}

export function useAgents(showError: (msg: string) => void) {
  const queryClient = useQueryClient();
  const roster = useAgentRoster();
  const { agents, ancestors } = roster.data ?? EMPTY_ROSTER;
  const activeId = useStore((state) => state.activeId);
  const setActiveId = useStore((state) => state.setActiveId);
  const hydrated = useRef(false);
  const previousId = useRef<number | null>(null);
  const traversal = useRef(false);
  const liveSelected = agents.find((agent) => agent.agent_id === activeId);
  const selected = useQuery({
    queryKey: [...AGENT_DETAIL_QUERY_KEY, activeId],
    queryFn: activeId == null ? skipToken : ({ signal }) => api.getAgent(activeId, signal),
    enabled: activeId != null && liveSelected == null,
    staleTime: Infinity,
    gcTime: 0,
    retry: false,
  });
  const activeAgent = liveSelected ?? selected.data;
  const actionAgents = activeAgent && !liveSelected ? [...agents, activeAgent] : agents;
  const actions = useAgentActions(showError, actionAgents);

  useEffect(() => {
    if (roster.error) showError(`Agent roster: ${errMsg(roster.error)}`);
    if (selected.error) showError(`Selected agent: ${errMsg(selected.error)}`);
  }, [roster.error, selected.error, showError]);

  useEffect(() => {
    const raw = new URL(window.location.href).searchParams.get("agent_id");
    const remembered = readRememberedAgent();
    const candidate = [raw, remembered].map(Number).find((id) => Number.isSafeInteger(id) && id > 0);
    if (candidate != null) setActiveId(candidate);
    hydrated.current = true;
  }, [setActiveId]);

  // meiva:note
  // Switching agents is a history entry: Back opens the agent that was selected
  // before, Forward the one selected after. The entry the app loads on is
  // rewritten in place, so arriving never leaves a duplicate behind, and a
  // traversal never pushes the entry it is restoring - the popstate handler
  // marks it so this effect rewrites that entry instead of adding a second one.
  // Each entry keeps its own copy of the router's state object rather than
  // sharing one (user report 2026-10-20).
  // meiva:link ui/web/src/docs/frontend-state/navigation-history.meiva.okf.md
  useEffect(() => {
    if (!hydrated.current) return;
    const id = useStore.getState().activeId;
    const previous = previousId.current;
    previousId.current = id;
    rememberAgent(id);
    const url = new URL(window.location.href);
    const selected = id == null ? null : String(id);
    if (url.searchParams.get("agent_id") !== selected) {
      if (selected == null) url.searchParams.delete("agent_id");
      else url.searchParams.set("agent_id", selected);
      url.searchParams.delete(REFERENCE_PARAM);
    }
    const push = !traversal.current && previous != null && id != null && previous !== id;
    traversal.current = false;
    if (url.toString() === window.location.href && !push) return;
    const state = { ...((window.history.state ?? {}) as Record<string, unknown>) };
    if (push) window.history.pushState(state, "", url);
    else window.history.replaceState(state, "", url);
  }, [activeId]);

  useEffect(() => {
    const restore = () => {
      const raw = new URL(window.location.href).searchParams.get("agent_id");
      const next = raw == null ? null : Number(raw);
      if (next !== null && !(Number.isSafeInteger(next) && next > 0)) return;
      if (next === useStore.getState().activeId) return;
      traversal.current = true;
      setActiveId(next);
    };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, [setActiveId]);

  useEffect(() => {
    if (!roster.isSuccess || useStore.getState().activeId != null) return;
    if (agents.length) setActiveId(agents[0].agent_id);
  }, [agents, roster.isSuccess, setActiveId]);

  const refresh = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: AGENTS_QUERY_KEY });
  }, [queryClient]);
  return { agents, ancestors, activeAgent, activeId, setActiveId, isLoading: roster.isLoading, refresh, ...actions };
}
