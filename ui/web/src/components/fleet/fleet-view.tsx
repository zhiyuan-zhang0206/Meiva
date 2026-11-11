// meiva:note
// Fleet view — full-screen supervision surface. Read-only over the agent and
// task graphs.
//
// The thesis: a human should be able to judge the work of 10-20 agents WITHOUT
// opening any conversation. The agents surface is the force-directed
// relationship Graph: spawn/fork/resurrect lineage as the structural skeleton,
// plus decaying agent-to-agent message traffic. Each node shows its current
// status and label and links straight to its conversation page for a
// drill-down.
//
// Responsive: on desktop (>= lg / 1024px) the graph panel fills the page. On
// mobile (< lg) the surfaces — Agents (graph) and Tasks (task graph) — become
// full-screen tabs, each filling the viewport, with a tab bar at the bottom to
// switch between them.
// meiva:link ui/web/src/docs/frontend-components/fleet-view.md

"use client";

import { MessageSquare } from "lucide-react";
import type { Route } from "next";
import Link from "next/link";
import { useTranslations } from "next-intl";
import { memo, useEffect, useMemo, useRef, useState } from "react";

import { GraphView } from "@/components/fleet/graph-view";
import { LeftGraphPanel } from "@/components/fleet/left-graph-panel";
import { readFleetRouteIds } from "@/components/fleet/fleet-route";
import { TaskGraph } from "@/components/fleet/task-graph";
import { PluginNavIcons } from "@/components/plugins/plugin-nav";
import type { AgentRow } from "@/lib/contracts/types";
import { useFleetAgents } from "@/lib/agents/use-fleet-agents";
import { useBreakpoint } from "@/lib/layout/breakpoint";
import { cn } from "@/lib/format/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";
import { useBrowser } from "@/lib/browser/browser-provider";

// meiva:note
// Which mobile tab is on screen is an EPHEMERAL, per-device selection ("which
// surface am I looking at right now"), not a durable preference — so it stays
// in localStorage rather than syncing to the DB across frontends. Written
// post-mount only (SSR renders the default; reading localStorage during render
// would mismatch hydration). A task deep link is the exception: it writes the
// tab because it is the user's current surface choice.
const LS_MOBILE_TAB = "meiva.fleet.mobileTab";
// meiva:note
// The desktop left-panel view (Graph vs Task Graph) is a durable preference —
// DB-backed (display.fleet_left_view) so it follows the user across frontends.

type MobileTab = "agents" | "tasks";

function isMobileTab(value: string): value is MobileTab {
  return value === "agents" || value === "tasks";
}
export function FleetView({ navigate }: { navigate: (href: Route) => void }) {
  const agents = useFleetAgents();
  const { isLarge } = useBreakpoint();
  // meiva:note
  // Cross-component selection shared by the Graph and the Task Graph.
  const [selectedAgentId, setSelectedAgentId] = useState<number | null>(null);
  const [selectedTaskId, setSelectedTaskId] = useState<number | null>(null);
  // meiva:note
  // Route jump from an inspector widget button (task #2909).
  const [routeTaskId, setRouteTaskId] = useState<number | null>(null);
  // meiva:note
  // Mobile tab selection (Agents | Tasks).
  const [mobileTab, setMobileTab] = useState<MobileTab>("agents");

  // meiva:note
  // Restore persisted state once after mount (avoids SSR hydration mismatch). The tab read answers
  // one of three things: `found` is the stored choice, `absent` leaves the default ("agents")
  // standing, and `unavailable` only means the tab is not remembered across visits — this visit
  // still works.
  // meiva:link ui/web/src/docs/frontend-state/browser-port.md
  const browser = useBrowser();
  const storage = browser.local;
  const navigation = browser.navigation;
  const hydrated = useRef(false);
  useEffect(() => {
    const stored = storage.read(LS_MOBILE_TAB);
    if (stored.state === "found" && isMobileTab(stored.value)) {
      // meiva:note
      // eslint-disable-next-line react-hooks/set-state-in-effect -- SSR-safe localStorage hydration: must run once after mount to avoid a hydration mismatch
      setMobileTab(stored.value);
    }
    hydrated.current = true;
  }, [storage]);
  useEffect(() => {
    const where = navigation.location();
    if (where === null) return;
    const search = new URLSearchParams(where.search);
    const route = readFleetRouteIds(search);
    if (route.taskId != null) {
      // meiva:note
      // eslint-disable-next-line react-hooks/set-state-in-effect -- Route state is hydrated after mount to avoid an SSR mismatch.
      setSelectedTaskId(route.taskId);
      setRouteTaskId(route.taskId);
      setMobileTab("tasks");
    }
  }, [navigation]);
  useEffect(() => {
    if (hydrated.current) storage.write(LS_MOBILE_TAB, mobileTab);
  }, [mobileTab, storage]);

  const terminatedCount = useMemo(
    () => agents.filter((a) => a.status === "terminated").length,
    [agents],
  );
  const aliveCount = agents.length - terminatedCount;

  return (
    // meiva:note
    // Task #1066: the root must FILL main's width (flex-1), not shrink to
    // its content's max-content — main is a row-flex landmark, so without
    // flex-1 a short surface collapses the whole page to the content width.
    // meiva:note
    // Fill `<main>` with `h-full` rather than `h-dvh`: on iOS/iPadOS `dvh` ignores the on-screen
    // keyboard, whereas `h-full` shrinks with `<html>` (`VisualViewportHeightSync`).
    <div className={cn("h-full bg-background text-foreground font-mono", FLEX, FLEX_COL, FLEX_1, MIN_H_0, MIN_W_0)}>
      {isLarge ? (
        <DesktopLayout
          aliveCount={aliveCount}
          agents={agents}
          selectedAgentId={selectedAgentId}
          setSelectedAgentId={setSelectedAgentId}
          selectedTaskId={selectedTaskId}
          setSelectedTaskId={setSelectedTaskId}
          routeTaskId={routeTaskId}
          navigate={navigate}
        />
      ) : (
        <MobileLayout
          aliveCount={aliveCount}
          agents={agents}
          selectedAgentId={selectedAgentId}
          setSelectedAgentId={setSelectedAgentId}
          selectedTaskId={selectedTaskId}
          setSelectedTaskId={setSelectedTaskId}
          mobileTab={mobileTab}
          setMobileTab={setMobileTab}
          routeTaskId={routeTaskId}
          navigate={navigate}
        />
      )}
    </div>
  );
}

// meiva:note
// ── Desktop layout (>= lg): the graph panel fills the page ──

const DesktopLayout = memo(function DesktopLayout({
  aliveCount,
  agents,
  selectedAgentId,
  setSelectedAgentId,
  selectedTaskId,
  setSelectedTaskId,
  routeTaskId,
  navigate,
}: {
  aliveCount: number;
  agents: AgentRow[];
  selectedAgentId: number | null;
  setSelectedAgentId: (id: number | null) => void;
  selectedTaskId: number | null;
  setSelectedTaskId: (id: number | null) => void;
  routeTaskId: number | null;
  navigate: (href: Route) => void;
}) {
  const t = useTranslations("fleet");
  return (
    <>
      <header className={cn("shrink-0 items-center gap-3 border-b border-border px-6 py-3", FLEX)}>
        <h1 className="text-sm font-semibold">{t("title")}</h1>
        <span className="text-xs text-muted-foreground tabular-nums">
          {t("activeTotal", { active: aliveCount, total: agents.length })}
        </span>
        {/* Plugin-contributed toolbar entries (contributions.ui.nav, location
            "fleet-toolbar"); renders nothing when none are declared. */}
        <div className="ml-auto">
          <PluginNavIcons location="fleet-toolbar" />
        </div>
        <Link
          href="/"
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
          aria-label={t("backToConversation")}
        >
          <MessageSquare className="size-5" aria-hidden />
        </Link>
      </header>

      <div className={cn(FLEX_1, MIN_H_0, MIN_W_0)}>
        <LeftGraphPanel
          selectedAgentId={selectedAgentId}
          setSelectedAgentId={setSelectedAgentId}
          selectedTaskId={selectedTaskId}
          setSelectedTaskId={setSelectedTaskId}
          routeTaskId={routeTaskId}
          navigate={navigate}
        />
      </div>
    </>
  );
});

// ── Mobile layout (< lg): full-screen tabs ──

const MobileLayout = memo(function MobileLayout({
  aliveCount,
  agents,
  selectedAgentId,
  setSelectedAgentId,
  selectedTaskId,
  setSelectedTaskId,
  mobileTab,
  setMobileTab,
  routeTaskId,
  navigate,
}: {
  aliveCount: number;
  agents: AgentRow[];
  selectedAgentId: number | null;
  setSelectedAgentId: (id: number | null) => void;
  selectedTaskId: number | null;
  setSelectedTaskId: (id: number | null) => void;
  mobileTab: MobileTab;
  setMobileTab: (t: MobileTab) => void;
  routeTaskId: number | null;
  navigate: (href: Route) => void;
}) {
  const t = useTranslations("fleet");
  return (
    <>
      {/* Mobile header (compact) */}
      <header className={cn("shrink-0 items-center gap-2 border-b border-border px-4 py-2", FLEX)}>
        <h1 className="text-sm font-semibold">{t("title")}</h1>
        <span className="text-[11px] text-muted-foreground tabular-nums">
          {t("activeTotal", { active: aliveCount, total: agents.length })}
        </span>
        <div className="ml-auto">
          <PluginNavIcons location="fleet-toolbar" />
        </div>
        <Link
          href="/"
          className="p-1 rounded text-muted-foreground hover:bg-sidebar-accent hover:text-foreground"
          aria-label={t("backToConversation")}
        >
          <MessageSquare className="size-4" aria-hidden />
        </Link>
      </header>

      {/* Tab content — fills all available space above the tab bar.
           One surface mounted at a time (R4 layer 4: conditional render). */}
      <div className={cn("relative", FLEX_1, MIN_H_0, MIN_W_0)}>
        {mobileTab === "agents" ? (
          <div className="absolute inset-0">
            <GraphView
              selectedAgentId={selectedAgentId}
              onSelectAgent={setSelectedAgentId}
              navigate={navigate}
            />
          </div>
        ) : null}
        {mobileTab === "tasks" ? (
          <div className="absolute inset-0">
            <TaskGraph
              selectedTaskId={selectedTaskId}
              onSelectTask={setSelectedTaskId}
              selectedAgentId={selectedAgentId}
              onSelectAgent={setSelectedAgentId}
              routeTaskId={routeTaskId}
              navigate={navigate}
            />
          </div>
        ) : null}
      </div>

      {/* Tab bar — fixed at the bottom */}
      <div
        className={cn(
          "shrink-0 items-center border-t border-border bg-background pb-[var(--safe-area-bottom,env(safe-area-inset-bottom,0px))]",
          FLEX,
        )}
        role="tablist"
        aria-label={t("sections")}
      >
        <MobileTabButton
          label={t("agents")}
          count={aliveCount}
          active={mobileTab === "agents"}
          onClick={() => setMobileTab("agents")}
        />
        <MobileTabButton
          label={t("tasks")}
          count={0}
          active={mobileTab === "tasks"}
          onClick={() => setMobileTab("tasks")}
        />
      </div>
    </>
  );
});

// ── Shared sub-components ──

// Mobile tab button — full-width, equal share; same visual vocabulary as TabButton.
function MobileTabButton({
  label,
  count,
  active,
  onClick,
}: {
  label: string;
  count: number;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="tab"
      aria-selected={active}
      onClick={onClick}
      className={cn(
        "items-center justify-center gap-1.5 py-3 text-xs font-medium transition-colors",
        active
          ? "bg-sidebar-accent text-foreground border-t-2 border-primary -mt-px"
          : "text-muted-foreground hover:text-foreground border-t-2 border-transparent",
          FLEX_1, FLEX
      )}
    >
      {label}
      {count > 0 && (
        <span className="rounded-full bg-muted px-1.5 text-[10px] tabular-nums text-foreground">
          {count}
        </span>
      )}
    </button>
  );
}
