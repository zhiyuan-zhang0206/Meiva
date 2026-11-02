"use client";

// meiva:note
// Cold entry waits 400ms so pointers sweeping across the tree never flash tooltips; once a card
// opens, moving to another row within 300ms skips the delay for sequential scanning, and leaving
// closes the pointer-events-none card immediately.
// meiva:link ui/web/src/docs/frontend-components/agent-sidebar.meiva.okf.md

import * as Popover from "@radix-ui/react-popover";
import { useTranslations } from "next-intl";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";

import { formatRelativeTime } from "@/lib/agents/sidebar";
import type { AgentRow, PublicAgentStatus } from "@/lib/contracts/types";
import { useWallDate } from "@/lib/format/now/use-wall-date";
import { useTimeFormat } from "@/lib/format/use-time-format";
import { cn } from "@/lib/format/utils";
import { harnessLabel } from "@/lib/harness/harness";
import { useCoarsePointer } from "@/lib/input/use-coarse-pointer";
import { FLEX, MIN_W_0 } from "@/lib/layout/layout";
import { PRIORITY_BG } from "@/lib/notifications/notices";

export const AGENT_HOVER_OPEN_DELAY_MS = 400;
export const AGENT_HOVER_WARMUP_MS = 300;

export const STATUS_DOT: Record<PublicAgentStatus, string> = {
  running: "bg-sky-500",
  idling: "bg-emerald-500",
  impersonated: "bg-indigo-400",
  terminated: "bg-destructive",
};

type AgentRowKey = Parameters<ReturnType<typeof useTranslations<"agentRow">>>[0];

const STATUS_LABEL_KEY: Record<PublicAgentStatus, AgentRowKey> = {
  running: "statusRunning",
  idling: "statusIdling",
  impersonated: "statusImpersonated",
  terminated: "statusTerminated",
};

interface HoverWarmupCoordinator {
  isWarm: boolean;
  cooldownTimer: ReturnType<typeof setTimeout> | null;
  activeAgentId: number | null;
  closeActive: (() => void) | null;
}

const AgentHoverGroupContext =
  createContext<RefObject<HoverWarmupCoordinator> | null>(null);

export function AgentHoverGroup({ children }: { children: ReactNode }) {
  const coordinatorRef = useRef<HoverWarmupCoordinator>({
    isWarm: false,
    cooldownTimer: null,
    activeAgentId: null,
    closeActive: null,
  });

  useEffect(() => {
    const coord = coordinatorRef.current;
    return () => {
      if (coord.cooldownTimer != null) {
        clearTimeout(coord.cooldownTimer);
        coord.cooldownTimer = null;
      }
    };
  }, []);

  return (
    <AgentHoverGroupContext.Provider value={coordinatorRef}>
      {children}
    </AgentHoverGroupContext.Provider>
  );
}

export interface AgentRowHoverHandlers {
  hoverOpen: boolean;
  onMouseEnter: () => void;
  onMouseLeave: () => void;
  dismissHover: () => void;
}

export function useAgentRowHover(
  agentId: number,
  disabled: boolean,
): AgentRowHoverHandlers {
  const coarsePointer = useCoarsePointer();
  const hoverDisabled = disabled || coarsePointer;
  const groupCoordinatorRef = useContext(AgentHoverGroupContext);
  const localCoordinatorRef = useRef<HoverWarmupCoordinator>({
    isWarm: false,
    cooldownTimer: null,
    activeAgentId: null,
    closeActive: null,
  });
  const coordinatorRef = groupCoordinatorRef ?? localCoordinatorRef;

  const [hoverOpen, setHoverOpen] = useState(false);
  const openTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  const clearOpenTimer = useCallback(() => {
    if (openTimerRef.current != null) {
      clearTimeout(openTimerRef.current);
      openTimerRef.current = null;
    }
  }, []);

  const activateRow = () => {
    const coordinator = coordinatorRef.current;
    if (coordinator.activeAgentId !== null && coordinator.activeAgentId !== agentId) {
      coordinator.closeActive?.();
    }
    coordinator.isWarm = true;
    coordinator.activeAgentId = agentId;
    coordinator.closeActive = () => setHoverOpen(false);
    setHoverOpen(true);
  };

  const onMouseEnter = () => {
    if (hoverDisabled) return;
    clearOpenTimer();
    const coordinator = coordinatorRef.current;
    if (coordinator.cooldownTimer != null) {
      clearTimeout(coordinator.cooldownTimer);
      coordinator.cooldownTimer = null;
    }
    if (coordinator.isWarm) {
      activateRow();
      return;
    }
    openTimerRef.current = setTimeout(() => {
      openTimerRef.current = null;
      activateRow();
    }, AGENT_HOVER_OPEN_DELAY_MS);
  };

  const onMouseLeave = () => {
    clearOpenTimer();
    const coordinator = coordinatorRef.current;
    const wasActive = hoverOpen || coordinator.activeAgentId === agentId;
    if (wasActive) {
      setHoverOpen(false);
      if (coordinator.activeAgentId === agentId) {
        coordinator.activeAgentId = null;
        coordinator.closeActive = null;
      }
      if (coordinator.cooldownTimer != null) {
        clearTimeout(coordinator.cooldownTimer);
      }
      coordinator.cooldownTimer = setTimeout(() => {
        coordinator.cooldownTimer = null;
        coordinator.isWarm = false;
      }, AGENT_HOVER_WARMUP_MS);
    }
  };

  const dismissHover = () => {
    clearOpenTimer();
    setHoverOpen(false);
    const coordinator = coordinatorRef.current;
    if (coordinator.activeAgentId === agentId) {
      coordinator.activeAgentId = null;
      coordinator.closeActive = null;
    }
    if (coordinator.cooldownTimer != null) {
      clearTimeout(coordinator.cooldownTimer);
      coordinator.cooldownTimer = null;
    }
    coordinator.isWarm = false;
  };

  useEffect(() => {
    const coordinator = coordinatorRef.current;
    return () => {
      clearOpenTimer();
      if (coordinator.activeAgentId === agentId) {
        coordinator.activeAgentId = null;
        coordinator.closeActive = null;
      }
    };
  }, [agentId, clearOpenTimer, coordinatorRef]);

  return {
    hoverOpen: hoverOpen && !hoverDisabled,
    onMouseEnter,
    onMouseLeave,
    dismissHover,
  };
}

function parseSpawnerAgentId(spawner: string): number | null {
  if (!spawner.startsWith("agent:")) return null;
  const parsed = Number(spawner.slice("agent:".length));
  return Number.isFinite(parsed) ? parsed : null;
}

export function AgentHoverCard({
  agent,
  displayLabel,
}: {
  agent: AgentRow;
  displayLabel: string;
}) {
  const t = useTranslations("agentRow");
  const now = useWallDate();
  const { formatFuture } = useTimeFormat();

  const offline = agent.liveness_state === "offline";
  const launchFailed = agent.availability?.reason.startsWith("launch_") === true;
  const activeTime = formatRelativeTime(agent.last_active_at, now);
  const spawnedTime = formatRelativeTime(agent.spawned_at, now);
  const spawnerAgentId = parseSpawnerAgentId(agent.spawner);
  const pausedUntilMs =
    agent.heartbeat_paused_until != null
      ? new Date(agent.heartbeat_paused_until).getTime()
      : NaN;
  const heartbeatPaused =
    agent.heartbeat_paused_until != null &&
    Number.isFinite(pausedUntilMs) &&
    pausedUntilMs > now.getTime();

  return (
    <Popover.Portal>
      <Popover.Content
        role="tooltip"
        data-testid="agent-hover-card"
        side="right"
        align="start"
        sideOffset={8}
        collisionPadding={8}
        onOpenAutoFocus={(e) => e.preventDefault()}
        onCloseAutoFocus={(e) => e.preventDefault()}
        className="pointer-events-none z-50 w-60 rounded-lg border border-border bg-popover/95 p-2.5 text-popover-foreground shadow-xl backdrop-blur outline-none"
      >
        <p className="font-mono text-xs font-semibold leading-snug break-words">
          {displayLabel}
        </p>

        <div className="mt-1.5 space-y-1 text-[11px]">
          <p className={cn("items-center gap-1.5 text-muted-foreground", MIN_W_0, FLEX)}>
            <span
              className={cn(
                "size-1.5 rounded-full shrink-0",
                offline ? "bg-muted-foreground/50" : STATUS_DOT[agent.status],
              )}
            />
            <span className="text-popover-foreground">
              {t(STATUS_LABEL_KEY[agent.status])}
            </span>
            <span aria-hidden>·</span>
            <span>{harnessLabel(agent.harness)}</span>
            <span aria-hidden>·</span>
            <span className="truncate font-mono">{agent.machine}</span>
          </p>

          <p className="tabular-nums text-muted-foreground">
            {t("hoverActive", { time: activeTime })} · {t("hoverSpawned", { time: spawnedTime })}
          </p>

          {agent.fork_source_agent_id != null ? (
            <p className="font-mono text-[10.5px] text-muted-foreground">
              {t("hoverForkedFrom", { id: agent.fork_source_agent_id })}
            </p>
          ) : spawnerAgentId != null ? (
            <p className="font-mono text-[10.5px] text-muted-foreground">
              {t("hoverSpawnedBy", { id: spawnerAgentId })}
            </p>
          ) : null}

          {offline ? (
            <p className="text-[10.5px] text-muted-foreground">
              {t("statusOffline")}
            </p>
          ) : null}

          {launchFailed ? (
            <p className="text-[10.5px] text-destructive">
              {t("launchFailed")}
            </p>
          ) : null}

          {agent.awaiting_response_count > 0 ? (
            <p className={cn("items-center gap-1.5 text-[10.5px]", FLEX)}>
              <span
                className={cn(
                  "size-1.5 rounded-full shrink-0",
                  agent.highest_notice_priority
                    ? PRIORITY_BG[agent.highest_notice_priority]
                    : "bg-sky-500",
                )}
              />
              <span>
                {t("hoverAwaitingReply", { count: agent.awaiting_response_count })}
              </span>
            </p>
          ) : null}

          {heartbeatPaused && agent.heartbeat_paused_until != null ? (
            <p className="text-[10.5px] text-muted-foreground">
              {t("hoverHeartbeatPaused", {
                time: formatFuture(agent.heartbeat_paused_until, now),
              })}
            </p>
          ) : null}
        </div>
      </Popover.Content>
    </Popover.Portal>
  );
}
