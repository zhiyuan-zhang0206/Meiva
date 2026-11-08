import { useMemo, useState, type RefObject } from "react";

import type { BackendTimelineItem } from "@/lib/contracts/types";
import { canonicalBufferCoordinate, type CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { useUserSettings } from "@/lib/state/use-user-settings";
import { parseItemIdParts } from "@/lib/timeline/timeline";

import { LiveClockGate } from "../model/reasoning-clock";
import { classifyItem, inboundKind } from "../model/runs";
import { groupTimelineSegments, type RankedTimelineGroup } from "../model/segments";
import type { CompactPin } from "../model/use-compact-transition-anchor";
import { useTimelineWindow, useTimelineWindowLimits } from "../model/use-timeline-window";
import { TimelineRow, cardConfigFor } from "../row";
import { PendingLiveActivityWindow, TurnBlock } from "../run-block";

export interface GroupEntry {
  group: RankedTimelineGroup;
  groupKey: string;
  virtualKey: string;
  isLiveTurn: boolean;
  isLastTurn: boolean;
  runExpanded: boolean;
}

interface TurnKeySnapshot {
  threadKey: string | undefined;
  groups: RankedTimelineGroup[];
  bufferEpoch: number | undefined;
  nextSeq: number;
  virtualKeys: readonly string[];
  turnOverrideKeys: readonly string[];
  memberToVirtualKey: ReadonlyMap<string, string>;
  memberToOverrideKey: ReadonlyMap<string, string>;
}

function memberTurnIdentity(
  member: { source: "buffer" | "canonical"; rank: number; item: BackendTimelineItem },
  bufferEpoch: number | undefined,
): string {
  return `${member.source}:${member.source === "buffer" ? (bufferEpoch ?? "") : ""}:${member.rank}:${member.item.item_id}`;
}

// meiva:note
// Preserve TurnBlock React identity (virtualKey) and manual override key across
// front-of-turn mutations (follower 250-item trimming and older-page prepends).
// Walking groups and members from newest to oldest keeps an active trailing
// TurnBlock mounted with its measured inner row heights and scrollport intact
// even when its oldest item is trimmed or an adjacent head run merges in.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.meiva.okf.md
function computeTurnKeySnapshot(
  groups: RankedTimelineGroup[],
  threadKey: string | undefined,
  bufferEpoch: number | undefined,
  prev: TurnKeySnapshot | null,
): TurnKeySnapshot {
  const sameThread = prev !== null && prev.threadKey === threadKey;
  const prevVirtualMap = sameThread ? prev.memberToVirtualKey : null;
  const prevOverrideMap = sameThread ? prev.memberToOverrideKey : null;
  let nextSeq = sameThread ? prev.nextSeq : 0;

  const virtualKeys = new Array<string>(groups.length);
  const turnOverrideKeys = new Array<string>(groups.length);
  const claimedVirtual = new Set<string>();
  const claimedOverride = new Set<string>();
  const nextVirtualMap = new Map<string, string>();
  const nextOverrideMap = new Map<string, string>();

  for (let g = groups.length - 1; g >= 0; g--) {
    const group = groups[g];
    if (group.kind === "single") {
      const m = group.member;
      virtualKeys[g] = `${threadKey ?? ""}:${memberTurnIdentity(m, bufferEpoch)}`;
      turnOverrideKeys[g] = m.item.item_id;
      continue;
    }

    let resolvedVirtual: string | undefined;
    let resolvedOverride: string | undefined;
    if (prevVirtualMap && prevOverrideMap) {
      for (let i = group.members.length - 1; i >= 0; i--) {
        const memberId = memberTurnIdentity(group.members[i], bufferEpoch);
        const vCandidate = prevVirtualMap.get(memberId);
        const oCandidate = prevOverrideMap.get(memberId);
        if (
          vCandidate !== undefined &&
          oCandidate !== undefined &&
          !claimedVirtual.has(vCandidate) &&
          !claimedOverride.has(oCandidate)
        ) {
          resolvedVirtual = vCandidate;
          resolvedOverride = oCandidate;
          break;
        }
      }
    }

    const first = group.members[0];
    const firstIdentity = memberTurnIdentity(first, bufferEpoch);
    if (resolvedVirtual === undefined || resolvedOverride === undefined) {
      const baseVirtual = `${threadKey ?? ""}:turn:${firstIdentity}`;
      const baseOverride = first.source === "canonical" && first.rank === 0
        ? first.item.item_id
        : `turn:${firstIdentity}`;
      if (claimedVirtual.has(baseVirtual) || claimedOverride.has(baseOverride)) {
        const suffix = nextSeq++;
        resolvedVirtual = `${baseVirtual}:${suffix}`;
        resolvedOverride = `${baseOverride}:${suffix}`;
      } else {
        resolvedVirtual = baseVirtual;
        resolvedOverride = baseOverride;
      }
    }

    claimedVirtual.add(resolvedVirtual);
    claimedOverride.add(resolvedOverride);
    virtualKeys[g] = resolvedVirtual;
    turnOverrideKeys[g] = resolvedOverride;
    for (const m of group.members) {
      const memberId = memberTurnIdentity(m, bufferEpoch);
      nextVirtualMap.set(memberId, resolvedVirtual);
      nextOverrideMap.set(memberId, resolvedOverride);
    }
  }

  return {
    threadKey,
    groups,
    bufferEpoch,
    nextSeq,
    virtualKeys,
    turnOverrideKeys,
    memberToVirtualKey: nextVirtualMap,
    memberToOverrideKey: nextOverrideMap,
  };
}

export function useTimelineGroupWindow({
  canonicalItems,
  compactBuffer,
  currentItems,
  threadKey,
  turnOverrides,
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
  bufferedItems?: BackendTimelineItem[];
  currentItems: BackendTimelineItem[];
  threadKey: string | undefined;
  turnOverrides: ReadonlyMap<string, boolean>;
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
  const { settings } = useUserSettings();
  const expandWorkingBlock = settings["display.expand_working_block"] === true;

  const groups = useMemo(
    () => groupTimelineSegments(canonicalItems, compactBuffer, currentItems),
    [canonicalItems, compactBuffer, currentItems],
  );

  const bufferEpoch = compactBuffer?.epoch;
  const [turnKeySnapshot, setTurnKeySnapshot] = useState<TurnKeySnapshot>(() =>
    computeTurnKeySnapshot(groups, threadKey, bufferEpoch, null),
  );
  let activeTurnKeys = turnKeySnapshot;
  if (
    turnKeySnapshot.groups !== groups ||
    turnKeySnapshot.threadKey !== threadKey ||
    turnKeySnapshot.bufferEpoch !== bufferEpoch
  ) {
    activeTurnKeys = computeTurnKeySnapshot(groups, threadKey, bufferEpoch, turnKeySnapshot);
    setTurnKeySnapshot(activeTurnKeys);
  }

  const groupEntries = useMemo<GroupEntry[]>(() => groups.map((group, index) => {
    const groupKey = activeTurnKeys.turnOverrideKeys[index];
    const virtualKey = activeTurnKeys.virtualKeys[index];
    const isLastTurn = group === groups[groups.length - 1];
    // meiva:note
    // Manual toggles take precedence over the live expansion preference,
    // including when the turn settles.
    // meiva:link ui/web/src/docs/frontend-components/timeline-view/deep-collapse.meiva.okf.md
    const isLiveTurn = group.kind === "turn" && isLastTurn && turnActive;
    const runExpanded = group.kind === "turn" && (turnOverrides.has(groupKey)
      ? (turnOverrides.get(groupKey) ?? false)
      : isLiveTurn && expandWorkingBlock);
    return { group, groupKey, virtualKey, isLiveTurn, isLastTurn, runExpanded };
  }), [groups, activeTurnKeys, turnOverrides, turnActive, expandWorkingBlock]);

  const renderedRows = groupEntries.reduce(
    (total, { group, runExpanded, isLastTurn }) =>
      total + (group.kind === "turn" && runExpanded
        ? isLastTurn ? Math.min(group.members.length, turnRows) : group.members.length
        : 1),
    0,
  );
  const virtualEnabled = renderedRows > activationRows;
  const virtualGroups = useMemo(() => groupEntries.map(({ group, virtualKey, runExpanded, isLastTurn }) => {
    const members = group.kind === "single" ? [group.member] : group.members;
    return {
      key: virtualKey,
      rank: members[0].rank,
      itemIds: members.map((m) => m.item.item_id),
      memberRankKeys: members.map((m) => `${m.rank}:${m.item.item_id}`),
      estimatedHeight: group.kind === "turn" ? (runExpanded ? 264 : 32) : 80,
      expandedRows: group.kind === "turn" && runExpanded && !isLastTurn
        ? members.map((m) => `${virtualKey}:${m.source}:${m.rank}:${m.item.item_id}`)
        : null,
    };
  }), [groupEntries]);

  const compactPinnedId = compactPin && (() => {
    const bufferedCoordinates = new Map((compactBuffer?.rows ?? []).map((row) => [
      `${row.rank}:${row.item.item_id}`, canonicalBufferCoordinate(row),
    ]));
    for (const { group } of groupEntries) {
      const members = group.kind === "single" ? [group.member] : group.members;
      for (const member of members) {
        const coordinate = member.source === "buffer"
          ? bufferedCoordinates.get(`${member.rank}:${member.item.item_id}`)
          : parseItemIdParts(member.item.item_id);
        if (
          coordinate?.rank === compactPin.rank &&
          coordinate.msg === compactPin.msg &&
          coordinate.block === compactPin.block
        ) {
          return { id: member.item.item_id, rank: member.rank };
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

  return { groups, groupEntries, virtualEnabled, virtualRange, rowRange, turnRows };
}

export function TimelineGroupList({
  items,
  compactBuffer,
  streamingCode,
  turnActive,
  lastEventAt = 0,
  overrides,
  toggleExpanded,
  toggleTurn,
  onActivityAtBottomChange,
  activityScrollSeq,
  groups,
  groupEntries,
  virtualEnabled,
  virtualRange,
  rowRange,
  turnRows,
}: {
  items: BackendTimelineItem[];
  compactBuffer: CompactTransitionBuffer | null;
  streamingCode: boolean;
  turnActive: boolean;
  lastEventAt?: number;
  overrides: ReadonlySet<string>;
  toggleExpanded: (id: string, kind: BackendTimelineItem["kind"]) => void;
  toggleTurn: (id: string, currentlyExpanded: boolean) => void;
  onActivityAtBottomChange?: (atBottom: boolean) => void;
  activityScrollSeq?: number;
  groups: RankedTimelineGroup[];
  groupEntries: GroupEntry[];
  virtualEnabled: boolean;
  virtualRange: { start: number; end: number; before: number; after: number };
  rowRange: (index: number) => { start: number; end: number; before: number; after: number };
  turnRows: number;
}) {
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
    const config = cardConfigFor(item);
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
          onToggle={toggleExpanded}
        />
      );
    }

    const isPrimary = classifyItem(item) === "primary";
    const defaultExpanded = isPrimary || forceExpand ? true : config.fixedDefault;
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
        onToggle={toggleExpanded}
      />
    );
  };

  // meiva:note
  // Immediately after a human user submits a message (before the first reasoning
  // or tool item arrives), render the inline PendingLiveActivityWindow row so the
  // user gets instant visual feedback with zero layout shift.
  // meiva:link ui/web/src/docs/frontend-components/timeline-view/timeline-view.meiva.okf.md
  const lastGroup = groups.length > 0 ? groups[groups.length - 1] : null;
  const showPendingActivityWindow =
    turnActive &&
    (lastGroup === null ||
      (lastGroup.kind === "single" &&
        lastGroup.member.item.kind === "inbound_chat" &&
        inboundKind(lastGroup.member.item.source) === "human"));

  return (
    <>
      {virtualEnabled && virtualRange.before > 0 ? (
        <div data-timeline-spacer="before" style={{ height: virtualRange.before }} aria-hidden="true" />
      ) : null}
      <LiveClockGate value={turnActive}>
        {groupEntries.slice(virtualRange.start, virtualRange.end).map(({ group, groupKey, virtualKey, isLiveTurn, isLastTurn, runExpanded }, offset) => {
          const renderedGroup = (() => {
            if (group.kind === "single") {
              const { item, index, source, rank } = group.member;
              return renderRow(item, index, source, rank);
            }

            const first = group.members[0];
            const turnId = first.item.item_id;
            const shouldWindowTurn = isLastTurn && runExpanded && group.members.length > turnRows;
            const forceChildExpand = isLiveTurn;
            const rows = !isLastTurn ? rowRange(virtualRange.start + offset)
              : { start: 0, end: group.members.length, before: 0, after: 0 };
            return (
              <TurnBlock
                id={turnId}
                toggleKey={groupKey}
                memberIds={group.members.map((m) => m.item.item_id)}
                memberRanks={group.members.map((m) => `${m.rank}:${m.item.item_id}`)}
                memberSources={group.members.map((m) => `${m.source}:${m.rank}:${m.item.item_id}`)}
                summary={group.summary}
                expanded={runExpanded}
                bounded={isLastTurn}
                onToggle={toggleTurn}
                turnActive={turnActive && isLastTurn}
                lastEventAt={lastEventAt}
                liveWindow={isLiveTurn}
                onActivityAtBottomChange={isLiveTurn ? onActivityAtBottomChange : undefined}
                scrollToBottomSeq={isLiveTurn ? activityScrollSeq : undefined}
                windowedCount={shouldWindowTurn ? group.members.length : undefined}
                turnRows={shouldWindowTurn ? turnRows : undefined}
                renderWindowedRow={
                  shouldWindowTurn
                    ? (idx: number) => {
                        const m = group.members[idx];
                        return renderRow(m.item, m.index, m.source, m.rank, forceChildExpand);
                      }
                    : undefined
                }
                timelineSource={first.source}
                displayRank={first.rank}
              >
                {runExpanded && rows.before > 0 ? <div data-timeline-spacer="turn-before" style={{ height: rows.before }} aria-hidden="true" /> : null}
                {runExpanded && !shouldWindowTurn ? (
                  group.members.slice(rows.start, rows.end).map((m) => {
                    const rowKey = `${virtualKey}:${m.source}:${m.rank}:${m.item.item_id}`;
                    return (
                      <div key={rowKey} data-virtual-row={!isLastTurn ? rowKey : undefined}>
                        {renderRow(m.item, m.index, m.source, m.rank, forceChildExpand)}
                      </div>
                    );
                  })
                ) : null}
                {runExpanded && rows.after > 0 ? <div data-timeline-spacer="turn-after" style={{ height: rows.after }} aria-hidden="true" /> : null}
              </TurnBlock>
            );
          })();
          return (
            <div key={virtualKey} data-virtual-group={virtualKey} className="py-1">
              {renderedGroup}
            </div>
          );
        })}
        {showPendingActivityWindow ? (
          <div className="py-1">
            <PendingLiveActivityWindow />
          </div>
        ) : null}
      </LiveClockGate>
      {virtualEnabled && virtualRange.after > 0 ? (
        <div data-timeline-spacer="after" style={{ height: virtualRange.after }} aria-hidden="true" />
      ) : null}
    </>
  );
}
