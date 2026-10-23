// Timeline reducer entry point — re-exports the pure fold domain
// (lib/fold/timeline.ts, extracted 2026-08-08, R4 layer 1) so the historical
// importers (tests, useTimeline, use-pending-messages) resolve unchanged.

export {
  applySystemEvent,
  clearCodeClocks,
  freezeExecClocks,
  freezeReasoningClocks,
  mergeSnapshotWithStreaming,
} from "../fold/timeline";

export {
  isEventForThread,
  isReattachedTimelineContext,
  parseItemId,
  parseItemIdParts,
  sortByItemId,
  standingHeadNoteIds,
} from "../fold";
