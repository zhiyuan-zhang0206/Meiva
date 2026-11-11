// meiva:note
// R4 layer 1 — questions-domain reducer (Task #1024).
//
// question_asked carries only the question id and title, and question_resolved only the id, so the
// unresolved-question list cannot be folded from events —
// the correct reconciliation is invalidation (refetch the questions query). This
// reducer centralizes that policy; use-questions.ts no longer subscribes.

import type { SystemEvent } from "../contracts/types";
import type { FoldOutcome } from "./types";
import { NO_FOLD } from "./types";

export const QUESTIONS_QUERY_KEY = ["questions"] as const;

export function foldQuestions(ev: SystemEvent): FoldOutcome {
  if (ev.role === "question_asked") {
    return { writes: [], invalidations: [{ key: QUESTIONS_QUERY_KEY }] };
  }
  if (ev.role === "question_resolved") {
    return { writes: [], invalidations: [{ key: QUESTIONS_QUERY_KEY }] };
  }
  return NO_FOLD;
}
