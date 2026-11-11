// meiva:note
// R4 layer 1 — notices-domain reducer (Task #1024).
//
// notice_posted / notice_resolved do NOT carry a full NoticeItem (no body, no
// timestamps), so the unresolved-question list cannot be folded from events —
// the correct reconciliation is invalidation (refetch the notices query). This
// reducer centralizes that policy; use-notices.ts no longer subscribes.

import type { SystemEvent } from "../contracts/types";
import type { FoldOutcome } from "./types";
import { NO_FOLD } from "./types";

export const NOTICES_QUERY_KEY = ["notices"] as const;

export function foldNotices(ev: SystemEvent): FoldOutcome {
  if (ev.role === "notice_posted") {
    return { writes: [], invalidations: [{ key: NOTICES_QUERY_KEY }] };
  }
  if (ev.role === "notice_resolved") {
    return { writes: [], invalidations: [{ key: NOTICES_QUERY_KEY }] };
  }
  return NO_FOLD;
}
