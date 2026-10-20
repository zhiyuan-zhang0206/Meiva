"use client";

import {
  Loader2,
} from "lucide-react";
import { useTranslations } from "next-intl";
import { useMemo } from "react";

import { AgentRow as AgentRowItem } from "@/components/agents/agent-row";
import { SpawnPopover } from "./spawn-popover";
import { AgentArchive } from "./archive";
import { ScrollArea } from "@/components/ui/scroll-area";
import { buildAgentTree, type AgentNode } from "@/lib/agents/agent-tree";
import { useSidebarSort, type FlatSortKey, type SidebarSort } from "@/lib/agents/sidebar";
import { useStore } from "@/lib/state/store";
import type { AgentRow } from "@/lib/contracts/types";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { cn } from "@/lib/format/utils";

import type { InnerProps } from "./types";
import { FLEX, FLEX_1, MIN_H_0 } from "@/lib/layout/layout";

// ── Expanded sidebar body ──

// Ascending comparator per sort key; direction is applied by sortAgentsFlat.
function compareAsc(a: AgentRow, b: AgentRow, key: FlatSortKey): number {
  switch (key) {
    case "id":
      return a.agent_id - b.agent_id;
    case "last_active": {
      const ta = a.last_active_at ? new Date(a.last_active_at).getTime() : 0;
      const tb = b.last_active_at ? new Date(b.last_active_at).getTime() : 0;
      return ta - tb;
    }
    case "status":
      return a.status.localeCompare(b.status);
  }
}

// Sort agents for flat list mode (key + direction).
function sortAgentsFlat(agents: AgentRow[], sort: SidebarSort): AgentRow[] {
  const sign = sort.dir === "asc" ? 1 : -1;
  return [...agents].sort((a, b) => sign * compareAsc(a, b, sort.key));
}


export function SidebarBody(props: InnerProps & { wide: boolean }) {
  const t = useTranslations("sidebar");
  const {
    agents,
    pendingSpawnCount,
    showTerminated,
    viewMode,
    onSpawn,
    isLoading,
  } = props;

  // searchQuery (store) drives the list filter below; the search INPUT itself
  // lives in the floating SearchOverlay (#723), not inline in the sidebar.
  const searchQuery = useStore((s) => s.searchQuery);
  const { sort } = useSidebarSort();
  // Quiet defaults (RCS): both are opt-in (`=== true`, default off).
  const { settings: userSettings } = useUserSettings();
  const showAgentStatus = userSettings["display.show_agent_status"] === true;


  const visibleAgents = agents;

  const isEmpty = visibleAgents.length === 0 && pendingSpawnCount === 0;
  const hasPending = pendingSpawnCount > 0;

  const rosterPending = isLoading;

  const treeProps: InnerProps & { wide: boolean } = hasPending
    ? { ...props, activeId: null }
    : props;

  // Compute flat sorted list (only used in flat mode).
  const flatAgents = useMemo(
    () => sortAgentsFlat(visibleAgents, sort),
    [visibleAgents, sort],
  );

  // The coherent roster carries links without loading historical cards.
  const tree = useMemo(
    () => buildAgentTree(agents, sort, { ancestors: props.ancestors }),
    [agents, sort, props.ancestors],
  );


  return (
    <>
      {/* ── Agent list ── */}
      <ScrollArea className={cn(FLEX_1, MIN_H_0)}>
        {rosterPending ? (
          <div className={cn("items-center justify-center gap-2 py-4 text-xs text-muted-foreground", FLEX)}>
            <Loader2 className="size-3.5 animate-spin" />
            {t("loadingAgents")}
          </div>
        ) : isEmpty ? (
          // meiva:note
          // Zero agents is the one moment the create action needs words: a bare
          // + does not explain itself to someone who has never spawned (the
          // header keeps the icon for every other visit).
          // meiva:link ui/web/src/docs/frontend-components/frontend-components.meiva.okf.md
          <div className="px-4 py-4 text-xs text-muted-foreground">
            {searchQuery ? t("noResults") : null}
            {!searchQuery && (
              <div className="space-y-2">
                <p>{t("emptyAgents")}</p>
                <SpawnPopover variant="cta" onSpawn={onSpawn} />
              </div>
            )}
          </div>
        ) : viewMode === "flat" ? (
          <ul className="py-1">
            {Array.from({ length: pendingSpawnCount }, (_, i) => (
              <SpawningRow key={`spawning-${i}`} active={i === 0} showStatus={showAgentStatus} />
            ))}
            {flatAgents.map((agent) => (
              <AgentRowItem
                key={agent.agent_id}
                agent={agent}
                label={agent.label ?? undefined}
                active={treeProps.activeId === agent.agent_id}
                pending={treeProps.pendingActions[agent.agent_id]}
                wide={treeProps.wide}
                depth={0}
                ancestorsIsLast={[]}
                onSelect={() => treeProps.onSelect(agent.agent_id)}
                onTerminate={() => treeProps.onTerminate(agent.agent_id)}
                onForceExpire={(sessionId) => treeProps.onForceExpire(agent.agent_id, sessionId)}
                onForceKill={() => treeProps.onTerminate(agent.agent_id, true)}
                onRestart={() => treeProps.onRestart(agent.agent_id)}
                onResurrect={(prompt) => treeProps.onResurrect(agent.agent_id, prompt)}
                onFork={(prompt) => treeProps.onFork(agent.agent_id, prompt)}
                onCompact={() => treeProps.onCompact(agent.agent_id)}
                onRename={(label) => treeProps.onRename(agent.agent_id, label)}
              />
            ))}
          </ul>
        ) : (
          <ul className="py-1">
            {Array.from({ length: pendingSpawnCount }, (_, i) => (
              <SpawningRow key={`spawning-${i}`} active={i === 0} showStatus={showAgentStatus} />
            ))}
            {tree.map((node) => (
              <TreeNode
                key={node.agent.agent_id}
                node={node}
                depth={0}
                ancestorsIsLast={[]}
                {...treeProps}
              />
            ))}
          </ul>
        )}
        {showTerminated && <AgentArchive props={treeProps} />}
      </ScrollArea>
    </>
  );
}

// Placeholder row for an in-flight spawn. The row itself is direct feedback to
// a user-initiated click, but its *motion* (pulse dot + spinner) counts as a
// dynamic signal and follows the status-color opt-in: quiet mode renders a
// neutral static placeholder instead.
function SpawningRow({ active, showStatus }: { active: boolean; showStatus: boolean }) {
  return (
    <li className="relative" data-testid="spawning-row">
      <div
        className={cn(
          "w-full text-left py-2.5 pr-4 font-mono text-xs items-center gap-2 border-l-2",
          active
            ? "bg-sidebar-accent text-sidebar-accent-foreground border-primary"
            : "text-muted-foreground border-transparent",
            FLEX
        )}
        style={{ paddingLeft: "16px" }}
      >
        {/* status dot follows the same opt-in as agent rows: hidden entirely
            when display.show_agent_status is off. */}
        {showStatus ? (
          <span className="size-1.5 rounded-full shrink-0 bg-slate-400 animate-pulse" />
        ) : null}
        <span className={cn("items-center min-h-4", FLEX_1, FLEX)}>
          {showStatus ? (
            <Loader2 className="size-3 animate-spin opacity-60" />
          ) : (
            <span className="text-[10px] opacity-60 leading-none">…</span>
          )}
        </span>
        <span className="text-[10px] opacity-0 shrink-0 tabular-nums">·</span>
        <span className="w-9 shrink-0" />
      </div>
    </li>
  );
}

interface TreeNodeProps extends InnerProps {
  wide: boolean;
  node: AgentNode;
  depth: number;
  ancestorsIsLast: readonly boolean[];
}

function TreeNode({ node, depth, ancestorsIsLast, ...rest }: TreeNodeProps) {
  const childAncestors = [...ancestorsIsLast, node.isLast];
  return (
    <>
      <AgentRowItem
        agent={node.agent}
        label={node.agent.label ?? undefined}
        active={rest.activeId === node.agent.agent_id}
        pending={rest.pendingActions[node.agent.agent_id]}
        wide={rest.wide}
        depth={depth}
        ancestorsIsLast={childAncestors}
        onSelect={() => rest.onSelect(node.agent.agent_id)}
        onTerminate={() => rest.onTerminate(node.agent.agent_id)}
        onForceExpire={(sessionId) => rest.onForceExpire(node.agent.agent_id, sessionId)}
        onForceKill={() => rest.onTerminate(node.agent.agent_id, true)}
        onRestart={() => rest.onRestart(node.agent.agent_id)}
        onResurrect={(prompt) => rest.onResurrect(node.agent.agent_id, prompt)}
        onFork={(prompt) => rest.onFork(node.agent.agent_id, prompt)}
        onCompact={() => rest.onCompact(node.agent.agent_id)}
        onRename={(label) => rest.onRename(node.agent.agent_id, label)}
      />
      {node.children.map((child) => (
        <TreeNode
          key={child.agent.agent_id}
          node={child}
          depth={depth + 1}
          ancestorsIsLast={childAncestors}
          {...rest}
        />
      ))}
    </>
  );
}
