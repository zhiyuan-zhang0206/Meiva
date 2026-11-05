import type { CompactTransitionBuffer } from "@/lib/layout/compact-transition";
import { parseItemIdParts } from "@/lib/timeline/timeline";
import type { BackendTimelineItem } from "@/lib/contracts/types";

import {
  classifyItem,
  isSilentTimelineItem,
  summarizeTurn,
  type TurnSummary,
} from "./runs";

export interface RankedTimelineItem {
  readonly item: BackendTimelineItem;
  readonly source: "buffer" | "canonical";
  readonly rank: number;
  readonly index: number;
}

export type RankedTimelineGroup =
  | {
      readonly kind: "single";
      readonly member: RankedTimelineItem;
    }
  | {
      readonly kind: "turn";
      readonly members: readonly RankedTimelineItem[];
      readonly summary: TurnSummary;
    };

// meiva:note
// Group the continuous display stream into maximal secondary TurnBlocks in one
// single pass across compact boundaries and transition buffers, while retaining
// each member's own (source, rank, index) for scroll anchors and DOM windowing.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/continuous-segments.meiva.okf.md
export function groupTimelineSegments(
  canonicalItems: readonly BackendTimelineItem[],
  compactBuffer: CompactTransitionBuffer | null = null,
  currentItems: readonly BackendTimelineItem[] = canonicalItems,
): RankedTimelineGroup[] {
  const ranked: RankedTimelineItem[] = compactBuffer
    ? [
        ...compactBuffer.rows.map((row, index) => ({
          item: row.item,
          source: "buffer" as const,
          rank: row.rank,
          index,
        })),
        ...currentItems.map((item, offset) => ({
          item,
          source: "canonical" as const,
          rank: parseItemIdParts(item.item_id)?.rank ?? 0,
          index: compactBuffer.rows.length + offset,
        })),
      ]
    : canonicalItems.map((item, index) => ({
        item,
        source: "canonical" as const,
        rank: parseItemIdParts(item.item_id)?.rank ?? 0,
        index,
      }));

  const groups: RankedTimelineGroup[] = [];
  let run: RankedTimelineItem[] = [];

  const flush = () => {
    if (run.length === 0) return;
    groups.push({
      kind: "turn",
      members: run,
      summary: summarizeTurn(run.map((m) => m.item)),
    });
    run = [];
  };

  for (const member of ranked) {
    if (isSilentTimelineItem(member.item)) continue;
    if (classifyItem(member.item) === "secondary") {
      run.push(member);
    } else {
      flush();
      groups.push({ kind: "single", member });
    }
  }
  flush();
  return groups;
}
