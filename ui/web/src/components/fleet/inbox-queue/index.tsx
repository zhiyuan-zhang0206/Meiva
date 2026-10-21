"use client";

import { useTranslations } from "next-intl";
import { memo, useEffect, useMemo, useRef, useState } from "react";

import { PRIORITY_RANK } from "@/lib/notifications/notices";
import { groupByTaskSubtree } from "@/lib/notifications/task-notify";
import type { AgentRow, PublicAgentStatus, NoticeItem, PageRow } from "@/lib/contracts/types";
import { useAllPages } from "@/lib/agents/use-all-pages";
import { useNotices } from "@/lib/notifications/use-notices";
import { useTasks } from "@/lib/notifications/use-tasks";
import { cn } from "@/lib/format/utils";

import { CompactDetail, OpenDetail, ResolvedDetail } from "./detail";
import { fmtLabel, openKey, resolvedKey, type OpenItem } from "./keys";
import { EmptyState, QueueHeader, QueueList } from "./list";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0, MIN_W_0 } from "@/lib/layout/layout";

function sortOpen(awaiting: NoticeItem[]): OpenItem[] {
  const items: OpenItem[] = [];
  for (const notice of awaiting) {
    items.push({ agentId: notice.agent_id, agentLabel: fmtLabel(notice.agent_label, notice.agent_id), notice });
  }
  items.sort((x, y) => {
    const p = PRIORITY_RANK[x.notice.priority] - PRIORITY_RANK[y.notice.priority];
    if (p !== 0) return p;
    if (x.notice.blocking !== y.notice.blocking) return x.notice.blocking ? -1 : 1;
    return x.notice.created_at.localeCompare(y.notice.created_at);
  });
  return items;
}


export const InboxQueue = memo(function InboxQueue({
  agents,
  className,
  selectedAgentId,
  onSelectAgent,
  compact,
  onCollapse,
  anchorAgentId,
  focusNoticeId,
}: {
  agents: AgentRow[];
  className?: string;
  selectedAgentId?: number | null;
  onSelectAgent?: (agentId: number | null) => void;
  compact?: boolean;
  onCollapse?: () => void;
  anchorAgentId?: number | null;
  focusNoticeId?: number | null;
}) {
  const t = useTranslations("fleet");
  const {
    awaiting,
    resolved,
    fetchNextPage,
    hasNextPage,
    isFetchingNextPage,
    error,
    resolvedError,
    isLoading,
  } = useNotices();
  const sortedItems = useMemo(
    () => sortOpen(awaiting),
    [awaiting],
  );

  // Task-subtree grouping. `openItems` is the DISPLAY order (groups pull their
  // members together), so selection defaults, auto-advance and arrow cycling all
  // follow what the eye sees, not the raw sort.
  const { tasks } = useTasks("7d", "summary");
  const units = useMemo(
    () =>
      groupByTaskSubtree(
        sortedItems,
        (it) => it.agentId,
        tasks,
        (it) => it.notice.task_id ?? null,
      ),
    [sortedItems, tasks],
  );
  const openItems = useMemo(() => units.flatMap((u) => u.items), [units]);

  // Agent status lookup — index by agent_id.
  const agentStatus = useMemo(() => {
    const m = new Map<number, PublicAgentStatus>();
    for (const a of agents) m.set(a.agent_id, a.status);
    return m;
  }, [agents]);

  // Each agent's single live page (meiva.ui.show), indexed by agent_id — the inbox
  // surfaces a direct entry to it. One fetch of GET /api/pages kept live by SSE
  // (useAllPages), so a many-agent queue never fans out per-row requests.
  const allPages = useAllPages();
  const activePageByAgent = useMemo(() => {
    const m = new Map<number, PageRow>();
    for (const p of allPages) if (!m.has(p.agent_id)) m.set(p.agent_id, p);
    return m;
  }, [allPages]);

  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const [resolvedOpen, setResolvedOpen] = useState(false);
  const [anchorHighlightKey, setAnchorHighlightKey] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement>(null);
  const attemptedAnchorRef = useRef<number | null>(null);
  const anchorFrameRef = useRef<number | null>(null);
  const anchorTimerRef = useRef<number | null>(null);

  // When an external selection arrives (tree/graph click), select the first open
  // notice from that agent so the list syncs bidirectionally.
  useEffect(() => {
    if (selectedAgentId == null) return;
    const cur = openItems.find((it) => openKey(it.notice.id) === selectedKey);
    if (cur?.agentId === selectedAgentId) return;
    const first = openItems.find((it) => it.agentId === selectedAgentId);
    if (first) setSelectedKey(openKey(first.notice.id));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- openItems changes on every refresh; only react to selectedAgentId
  }, [selectedAgentId]);

  // A route anchor is attempted once after the initial notice load. It only
  // reveals the row in its existing display position; it never changes queue
  // ordering or opens compact detail. A missing notice deliberately leaves the
  // queue at its normal default and is not retried on a later SSE update.
  useEffect(() => {
    if (
      anchorAgentId == null ||
      isLoading ||
      attemptedAnchorRef.current === anchorAgentId
    ) {
      return;
    }
    attemptedAnchorRef.current = anchorAgentId;
    const item = openItems.find((candidate) => candidate.agentId === anchorAgentId);
    if (!item) return;

    const key = openKey(item.notice.id);
    setAnchorHighlightKey(key);
    anchorFrameRef.current = window.requestAnimationFrame(() => {
      listRef.current
        ?.querySelector<HTMLElement>(`[data-notice-key="${key}"]`)
        ?.scrollIntoView({ block: "nearest" });
      anchorFrameRef.current = null;
    });
    anchorTimerRef.current = window.setTimeout(() => {
      setAnchorHighlightKey((current) => (current === key ? null : current));
      anchorTimerRef.current = null;
    }, 1_600);
  }, [anchorAgentId, isLoading, openItems]);

  useEffect(
    () => () => {
      if (anchorFrameRef.current !== null) {
        window.cancelAnimationFrame(anchorFrameRef.current);
      }
      if (anchorTimerRef.current !== null) {
        window.clearTimeout(anchorTimerRef.current);
      }
    },
    [],
  );

  // A route-driven jump (task #2909, the inspector's notification button)
  // opens the notice it names — once, after the queue loads, like the agent
  // anchor above. An open notice wins its exact row; a resolved one is shown
  // from the loaded history page. A notice in neither (gone, or beyond the
  // history page) leaves the default selection alone and is not retried. The
  // selection propagates its agent through the sync effect below.
  const attemptedFocusRef = useRef<number | null>(null);
  useEffect(() => {
    if (focusNoticeId == null || isLoading || attemptedFocusRef.current === focusNoticeId) {
      return;
    }
    attemptedFocusRef.current = focusNoticeId;
    if (openItems.some((item) => item.notice.id === focusNoticeId)) {
      setSelectedKey(openKey(focusNoticeId));
      return;
    }
    if (resolved.some((notice) => notice.id === focusNoticeId)) {
      setSelectedKey(resolvedKey(focusNoticeId));
    }
  }, [focusNoticeId, isLoading, openItems, resolved]);

  // Resolve the selection; default to the top open notice so the detail pane is
  // never blank while work is waiting. A just-resolved selection (dropped from
  // openItems by SSE) falls through to this default.
  const selectedOpen =
    openItems.find((it) => openKey(it.notice.id) === selectedKey) ??
    (selectedKey == null ? (openItems[0] ?? null) : null);
  const selectedResolved =
    selectedKey == null ? null : (resolved.find((n) => resolvedKey(n.id) === selectedKey) ?? null);
  const effectiveKey = selectedOpen
    ? openKey(selectedOpen.notice.id)
    : selectedResolved
      ? resolvedKey(selectedResolved.id)
      : null;

  const total = openItems.length;

  // Sync effective selection to the parent so the graph auto-focuses the right
  // node on tab switch / initial load.
  useEffect(() => {
    if (selectedOpen) {
      onSelectAgent?.(selectedOpen.agentId);
    } else if (selectedResolved) {
      onSelectAgent?.(selectedResolved.agent_id);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- effectiveKey derives from selectedOpen/selectedResolved
  }, [effectiveKey, onSelectAgent]);

  const listProps = {
    units,
    resolved,
    resolvedOpen,
    setResolvedOpen,
    agentStatus,
    activePageByAgent,
    onSelect: setSelectedKey,
    onSelectAgent,
    hasNextPage,
    isFetchingNextPage,
    fetchNextPage,
    listRef,
    anchorHighlightKey,
  };
  const empty = openItems.length === 0 && resolved.length === 0;
  // Error presentation (audit C3) — stale-while-error, useTasks style: a
  // failed fetch keeps its last data on screen and the failure is surfaced,
  // so "queue empty" is never indistinguishable from "couldn't load".
  const openFailed = error && openItems.length === 0; // open feed never loaded
  const historyFailed = resolvedError && resolved.length === 0; // history never loaded
  const stale = (error && openItems.length > 0) || (resolvedError && resolved.length > 0);

  // Compact (mobile): show list or detail full-screen, never both. An empty
  // selection means "show the list" — so this uses selectedKey (not the
  // auto-defaulting effectiveKey), and a selection whose notice just resolved
  // away falls back to the list.
  if (compact) {
    const openSel =
      selectedKey == null
        ? null
        : (openItems.find((it) => openKey(it.notice.id) === selectedKey) ?? null);
    return (
      <div className={cn("h-full", className, FLEX, FLEX_COL, MIN_H_0)}>
        {openSel == null && selectedResolved == null ? (
          <div className={cn("h-full", FLEX, FLEX_COL, MIN_H_0)}>
            <QueueHeader total={total} stale={stale} />
            {empty ? (
              <EmptyState failed={(error || resolvedError) && empty} loading={isLoading && empty} />
            ) : (
              <QueueList {...listProps} effectiveKey={null} openFailed={openFailed} historyFailed={historyFailed} />
            )}
          </div>
        ) : openSel ? (
          <CompactDetail label={t("inbox")} onBack={() => setSelectedKey(null)}>
            <OpenDetail
              key={openKey(openSel.notice.id)}
              item={openSel}
              openItems={openItems}
              agentStatus={agentStatus.get(openSel.agentId) ?? null}
              agentPage={activePageByAgent.get(openSel.agentId)}
              onAdvance={setSelectedKey}
              onSelectAgent={onSelectAgent}
            />
          </CompactDetail>
        ) : selectedResolved ? (
          <CompactDetail label={t("inbox")} onBack={() => setSelectedKey(null)}>
            <ResolvedDetail
              key={resolvedKey(selectedResolved.id)}
              notice={selectedResolved}
              agentStatus={agentStatus.get(selectedResolved.agent_id) ?? null}
            />
          </CompactDetail>
        ) : (
          <div className={cn("h-full items-center justify-center px-4 text-center text-xs text-muted-foreground", FLEX)}>
            {t("inboxPanel.selectNotice")}
          </div>
        )}
      </div>
    );
  }

  return (
    // A fixed per-viewport layout value, not a durable user preference —
    // never persisted.
    <div className={cn( className, FLEX)}>
      <div className={cn("h-full w-[42%] shrink-0", FLEX, FLEX_COL, MIN_H_0, MIN_W_0)}>
        <QueueHeader total={total} onCollapse={onCollapse} stale={stale} />
        {empty ? (
          <EmptyState failed={(error || resolvedError) && empty} loading={isLoading && empty} />
        ) : (
          <QueueList {...listProps} effectiveKey={effectiveKey} openFailed={openFailed} historyFailed={historyFailed} />
        )}
      </div>
      <div className="w-px shrink-0 border-l border-border" />
      <div className={cn(MIN_W_0, FLEX_1)}>
        {selectedOpen ? (
          <OpenDetail
            key={openKey(selectedOpen.notice.id)}
            item={selectedOpen}
            openItems={openItems}
            agentStatus={agentStatus.get(selectedOpen.agentId) ?? null}
            agentPage={activePageByAgent.get(selectedOpen.agentId)}
            onAdvance={setSelectedKey}
            onSelectAgent={onSelectAgent}
          />
        ) : selectedResolved ? (
          <ResolvedDetail
            key={resolvedKey(selectedResolved.id)}
            notice={selectedResolved}
            agentStatus={agentStatus.get(selectedResolved.agent_id) ?? null}
          />
        ) : (
          <div className={cn("h-full items-center justify-center px-4 text-center text-xs text-muted-foreground", FLEX)}>
            {t("inboxPanel.selectNotice")}
          </div>
        )}
      </div>
    </div>
  );
});
