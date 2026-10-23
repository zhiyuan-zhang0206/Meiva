import { useMemo, type RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { canonicalBufferCoordinate, type CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import type { DetailsMode } from "@/lib/layout/content-toggle-store";
import { parseItemIdParts } from "@/lib/timeline/timeline";
import { useTimelineColors } from "@/lib/timeline/use-timeline-colors";

import { LiveClockGate } from "../model/reasoning-clock";
import { classifyItem, type TimelineGroup } from "../model/runs";
import { groupTimelineSegments } from "../model/segments";
import type { CompactPin } from "../model/use-compact-transition-anchor";
import { useTimelineWindow, useTimelineWindowLimits } from "../model/use-timeline-window";
import { TimelineRow, cardConfigFor } from "../row";
import { TurnBlock } from "../run-block";

export interface GroupEntry {
  entry: {
    group: TimelineGroup;
    indexOffset: number;
    rank: number;
    source: "buffer" | "canonical";
  };
  groupKey: string;
  virtualKey: string;
  runExpanded: boolean;
  virtualRows: string[] | null;
}

export function useTimelineGroupWindow({
  canonicalItems,
  compactBuffer,
  bufferedItems,
  currentItems,
  threadKey,
  turnOverrides,
  effectiveDetailsMode,
  turnActive,
  viewportRef,
  contentRef,
  isFollowing,
  pendingAnchor,
  compactPin,
  restoreAnchor,
  restoreTop,
}: {
  canonicalItems: BackendTimelineItem[];
  compactBuffer: CompactTransitionBuffer | null;
  bufferedItems: BackendTimelineItem[];
  currentItems: BackendTimelineItem[];
  threadKey: string | undefined;
  turnOverrides: ReadonlyMap<string, boolean>;
  effectiveDetailsMode: DetailsMode;
  turnActive: boolean;
  viewportRef: RefObject<HTMLElement | null>;
  contentRef: RefObject<HTMLDivElement | null>;
  isFollowing: () => boolean;
  pendingAnchor: { id: string; rank: number } | null;
  compactPin: CompactPin | null;
  restoreAnchor: { id: string; rank: number } | null;
  restoreTop: number | null;
}) {
  const { activationRows, turnRows, measureRows } = useTimelineWindowLimits();

  const groups = useMemo(() => {
    if (!compactBuffer) {
      return groupTimelineSegments(canonicalItems).map((entry) => ({ ...entry, source: "canonical" as const }));
    }
    const buffered = groupTimelineSegments(
      bufferedItems,
      (_item, index) => compactBuffer.rows[index].rank,
    ).map((entry) => ({ ...entry, source: "buffer" as const }));
    const current = groupTimelineSegments(currentItems)
      .map((entry) => ({
        ...entry,
        indexOffset: entry.indexOffset + bufferedItems.length,
        source: "canonical" as const,
      }));
    return [...buffered, ...current];
  }, [canonicalItems, compactBuffer, bufferedItems, currentItems]);

  const groupEntries = useMemo<GroupEntry[]>(() => groups.map((entry) => {
    const group = entry.group;
    const groupKey = group.kind === "single" ? group.item.item_id : group.items[0].item_id;
    const virtualKey = `${threadKey ?? ""}:${entry.source}:${entry.source === "buffer" ? compactBuffer?.epoch : ""}:${entry.rank}:${groupKey}`;
    const isLastTurn = entry === groups[groups.length - 1];
    const runExpanded = group.kind === "turn" && (turnOverrides.has(groupKey)
      ? (turnOverrides.get(groupKey) ?? false)
      : effectiveDetailsMode === "all"
        ? true
        : effectiveDetailsMode === "last"
          ? isLastTurn && turnActive
          : false);
    const virtualRows = group.kind === "turn" && runExpanded
      ? group.items.map((item) => `${virtualKey}:${item.item_id}`)
      : null;
    return { entry, groupKey, virtualKey, runExpanded, virtualRows };
  }), [groups, compactBuffer?.epoch, turnOverrides, effectiveDetailsMode, turnActive, threadKey]);

  const renderedRows = groupEntries.reduce((total, entry) => total + (entry.virtualRows?.length ?? 1), 0);
  const virtualEnabled = renderedRows > activationRows;
  const virtualGroups = useMemo(() => groupEntries.map(({ entry, virtualKey, virtualRows }) => ({
    key: virtualKey,
    rank: entry.rank,
    itemIds: entry.group.kind === "single"
      ? [entry.group.item.item_id]
      : entry.group.items.map((item) => item.item_id),
    estimatedHeight: (entry.group.kind === "turn" && virtualRows
      ? 60 + virtualRows.length * 92
      : entry.group.kind === "turn" ? 56 : 80),
    expandedRows: virtualRows,
  })), [groupEntries]);

  const compactPinnedId = compactPin && (() => {
    const bufferedCoordinates = new Map((compactBuffer?.rows ?? []).map((row) => [
      `${row.rank}:${row.item.item_id}`, canonicalBufferCoordinate(row),
    ]));
    for (const { entry } of groupEntries) {
      for (const item of entry.group.kind === "single" ? [entry.group.item] : entry.group.items) {
        const coordinate = entry.source === "buffer"
          ? bufferedCoordinates.get(`${entry.rank}:${item.item_id}`)
          : parseItemIdParts(item.item_id);
        if (entry.rank === compactPin.rank && coordinate?.msg === compactPin.msg && coordinate.block === compactPin.block) {
          return { id: item.item_id, rank: entry.rank };
        }
      }
    }
    return null;
  })();

  const { range: virtualRange, rowRange } = useTimelineWindow({
    groups: virtualGroups,
    enabled: virtualEnabled,
    turnRows,
    measureRows,
    viewportRef,
    contentRef,
    isFollowing,
    identity: threadKey ?? null,
    pendingAnchor: pendingAnchor ?? compactPinnedId ?? restoreAnchor,
    restoreTop,
  });

  return { groups, groupEntries, virtualEnabled, virtualRange, rowRange };
}

export function TimelineGroupList({
  items,
  compactBuffer,
  streamingCode,
  turnActive,
  effectiveDetailsMode,
  overrides,
  activeStuckHeaderId,
  activeStuckChildId,
  toggleExpanded,
  toggleTurn,
  groups,
  groupEntries,
  virtualEnabled,
  virtualRange,
  rowRange,
}: {
  items: BackendTimelineItem[];
  compactBuffer: CompactTransitionBuffer | null;
  streamingCode: boolean;
  turnActive: boolean;
  effectiveDetailsMode: DetailsMode;
  overrides: ReadonlySet<string>;
  activeStuckHeaderId: string | null;
  activeStuckChildId: string | null;
  toggleExpanded: (id: string, kind: BackendTimelineItem["kind"]) => void;
  toggleTurn: (id: string, currentlyExpanded: boolean) => void;
  groups: GroupEntry["entry"][];
  groupEntries: GroupEntry[];
  virtualEnabled: boolean;
  virtualRange: { start: number; end: number; before: number; after: number };
  rowRange: (groupIndex: number) => { start: number; end: number; before: number; after: number };
}) {
  const timelineColors = useTimelineColors();

  const renderRow = (
    item: BackendTimelineItem,
    index: number,
    source: "buffer" | "canonical",
    rank: number,
    forceExpand?: boolean,
  ) => {
    const renderKey = source === "buffer"
      ? `buffer:${compactBuffer?.epoch}:${rank}:${item.item_id}`
      : `canonical:${item.item_id}`;
    const config = cardConfigFor(item, timelineColors);
    const streaming =
      streamingCode && index === items.length - 1 && item.kind === "agent_code";

    if (config === null) {
      return (
        <TimelineRow
          key={renderKey}
          timelineSource={source} displayRank={rank}
          item={item}
          config={null}
          streaming={streaming}
          expanded={false}
          copyable={false}
          isStuck={false}
          onToggle={toggleExpanded}
        />
      );
    }

    const isPrimary = classifyItem(item) === "primary";
    const isLastItem = index === items.length - 1;
    const defaultExpanded = isPrimary || forceExpand ? true
      : effectiveDetailsMode === "all" ? true
      : effectiveDetailsMode === "last" ? isLastItem
      : config.fixedDefault;
    const expanded = overrides.has(item.item_id) ? !defaultExpanded : defaultExpanded;
    const copyable = expanded && !item.partial;

    return (
      <TimelineRow
        key={renderKey}
        timelineSource={source} displayRank={rank}
        item={item}
        config={config}
        streaming={streaming}
        expanded={expanded}
        copyable={copyable}
        stickyHeader={isPrimary ? false : "nested"}
        isStuck={!isPrimary && activeStuckChildId === item.item_id}
        onToggle={toggleExpanded}
      />
    );
  };

  return (
    <>
      {virtualEnabled && virtualRange.before > 0 ? (
        <div data-timeline-spacer="before" style={{ height: virtualRange.before }} aria-hidden="true" />
      ) : null}
      <LiveClockGate value={turnActive}>
        {groupEntries.slice(virtualRange.start, virtualRange.end).map(({ entry, virtualKey, runExpanded, virtualRows }, offset) => {
          const group = entry.group;
          const groupIndex = virtualRange.start + offset;
          const renderedGroup = (() => {
            if (group.kind === "single") {
              return renderRow(group.item, entry.indexOffset + group.index, entry.source, entry.rank);
            }

            const turnId = group.items[0].item_id;
            const isLastTurn = entry === groups[groups.length - 1];
            const childRange = rowRange(groupIndex);
            return (
              <TurnBlock
                id={turnId}
                memberIds={group.items.map((it) => it.item_id)}
                summary={group.summary}
                expanded={runExpanded}
                onToggle={toggleTurn}
                turnActive={turnActive && isLastTurn}
                isStuck={activeStuckHeaderId === turnId}
                timelineSource={entry.source} displayRank={entry.rank}
              >
                {runExpanded ? (
                  virtualRows ? (
                    <>
                      {virtualEnabled && childRange.before > 0 ? <div data-timeline-spacer="turn-before" style={{ height: childRange.before }} aria-hidden="true" /> : null}
                      {group.items.slice(childRange.start, childRange.end).map((it, childOffset) => {
                        const index = childRange.start + childOffset;
                        return (
                          <div key={virtualRows[index]} data-virtual-row={virtualRows[index]}>
                            {renderRow(it, entry.indexOffset + group.startIndex + index, entry.source, entry.rank, effectiveDetailsMode !== "none")}
                          </div>
                        );
                      })}
                      {virtualEnabled && childRange.after > 0 ? <div data-timeline-spacer="turn-after" style={{ height: childRange.after }} aria-hidden="true" /> : null}
                    </>
                  ) : null
                ) : null}
              </TurnBlock>
            );
          })();
          return (
            <div key={virtualKey} data-virtual-group={virtualKey}>
              {renderedGroup}
            </div>
          );
        })}
      </LiveClockGate>
      {virtualEnabled && virtualRange.after > 0 ? (
        <div data-timeline-spacer="after" style={{ height: virtualRange.after }} aria-hidden="true" />
      ) : null}
    </>
  );
}
