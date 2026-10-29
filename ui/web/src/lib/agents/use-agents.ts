"use client";

// One coherent live tree; a selected historical conversation is a separate
// ID-addressed read. Neither selection nor lineage depends on loading history.
import { skipToken, useQuery, useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef } from "react";

import type { BrowserStorage } from "../browser/browser";
import { useBrowser } from "../browser/browser-provider";
import { REFERENCE_PARAM } from "../context/reference-url";
import { errMsg } from "../contracts/errors";
import type { AgentRoster } from "../contracts/types";
import { AGENT_DETAIL_QUERY_KEY, AGENTS_QUERY_KEY } from "../fold/agents";
import { useStore } from "../state/store";
import { useApi } from "../transport/transport-context";
import { useAgentActions } from "./use-agent-actions";

export { AGENTS_QUERY_KEY } from "../fold/agents";
export type { PendingAction } from "./use-agent-actions";

const EMPTY_ROSTER: AgentRoster = { agents: [], ancestors: [] };
const ACTIVE_ID_KEY = "meiva.active.agent_id";

// meiva:note
// The remembered selection is the page's local storage, read and written through the browser
// port. `absent` (never set here) and `unavailable` (a private window, disabled site data) both
// leave the selection working for the session; it just stops being remembered.
// meiva:link ui/web/src/docs/frontend-state/navigation-history.meiva.okf.md
function rememberedAgent(storage: BrowserStorage): string | null {
  const stored = storage.read(ACTIVE_ID_KEY);
  return stored.state === "found" ? stored.value : null;
}

function rememberAgent(storage: BrowserStorage, id: number | null): void {
  if (id === null) storage.remove(ACTIVE_ID_KEY);
  else storage.write(ACTIVE_ID_KEY, String(id));
}

export function useAgentRoster() {
  const client = useApi();
  return useQuery({
    queryKey: AGENTS_QUERY_KEY,
    queryFn: ({ signal }) => client.getAgentRoster(signal),
    staleTime: Infinity,
  });
}

export function useAgents(showError: (msg: string) => void) {
  const client = useApi();
  const queryClient = useQueryClient();
  // meiva:note
  // The page's address and its local storage enter through the browser port: the hook steers
  // history, deep links and the remembered selection by the values the port returns, and a test
  // hands in `fakeBrowser()` instead of arranging a global.
  // meiva:link ui/web/src/docs/frontend-state/navigation-history.meiva.okf.md
  const browser = useBrowser();
  const navigation = browser.navigation;
  const selectionStorage = browser.local;
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
    queryFn: activeId == null ? skipToken : ({ signal }) => client.getAgent(activeId, signal),
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
    const location = navigation.location();
    const raw = location === null ? null : new URLSearchParams(location.search).get("agent_id");
    const remembered = rememberedAgent(selectionStorage);
    const candidate = [raw, remembered].map(Number).find((id) => Number.isSafeInteger(id) && id > 0);
    if (candidate != null) setActiveId(candidate);
    hydrated.current = true;
  }, [navigation, selectionStorage, setActiveId]);

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
    rememberAgent(selectionStorage, id);
    const location = navigation.location();
    if (location === null) return;
    const url = new URL(location.href);
    const selected = id == null ? null : String(id);
    if (url.searchParams.get("agent_id") !== selected) {
      if (selected == null) url.searchParams.delete("agent_id");
      else url.searchParams.set("agent_id", selected);
      url.searchParams.delete(REFERENCE_PARAM);
    }
    const push = !traversal.current && previous != null && id != null && previous !== id;
    traversal.current = false;
    if (url.href === location.href && !push) return;
    const rawState: unknown = navigation.state();
    const state: Record<string, unknown> =
      typeof rawState === "object" && rawState !== null && !Array.isArray(rawState)
        ? { ...rawState }
        : {};
    if (push) navigation.push(url.href, state);
    else navigation.replace(url.href, state);
  }, [activeId, navigation, selectionStorage]);

  useEffect(() => {
    const restore = () => {
      const location = navigation.location();
      const raw = location === null ? null : new URLSearchParams(location.search).get("agent_id");
      const next = raw == null ? null : Number(raw);
      if (next !== null && !(Number.isSafeInteger(next) && next > 0)) return;
      if (next === useStore.getState().activeId) return;
      traversal.current = true;
      setActiveId(next);
    };
    return navigation.onTraverse(restore);
  }, [navigation, setActiveId]);

  useEffect(() => {
    if (!roster.isSuccess || useStore.getState().activeId != null) return;
    if (agents.length) setActiveId(agents[0].agent_id);
  }, [agents, roster.isSuccess, setActiveId]);

  const refresh = useCallback(async () => {
    await queryClient.invalidateQueries({ queryKey: AGENTS_QUERY_KEY });
  }, [queryClient]);
  return { agents, ancestors, activeAgent, activeId, setActiveId, isLoading: roster.isLoading, refresh, ...actions };
}
