// meiva:note
// What the answer form keeps while the user is elsewhere: per agent, per open question, the choices,
// text, Note and skip they have entered. Memory only. These are the pure updates the store applies:
// each returns the same object when nothing changes, so a subscriber is not woken for no reason.
// meiva:link ui/web/src/docs/frontend-state/shared-context/questions.md

export interface QuestionDraft {
  readonly optionIds: readonly string[];
  readonly text: string;
  readonly comment: string;
  readonly skipped: boolean;
}

export const EMPTY_DRAFT: QuestionDraft = { optionIds: [], text: "", comment: "", skipped: false };

export type AgentQuestionDrafts = Readonly<Partial<Record<number, QuestionDraft>>>;
export type QuestionDrafts = Readonly<Partial<Record<number, AgentQuestionDrafts>>>;

export function withDraft(
  all: QuestionDrafts,
  agentId: number,
  questionId: number,
  draft: QuestionDraft,
): QuestionDrafts {
  return { ...all, [agentId]: { ...all[agentId], [questionId]: draft } };
}

function withoutAgent(all: QuestionDrafts, agentId: number): QuestionDrafts {
  const { [agentId]: _dropped, ...rest } = all;
  return rest;
}

// meiva:note
// Keep only the entries of `keep` for one agent; an agent left with none disappears. An answered
// question and one that is no longer open leave no draft behind.
function keeping(all: QuestionDrafts, agentId: number, keep: (questionId: number) => boolean): QuestionDrafts {
  const current = all[agentId];
  if (current === undefined) return all;
  const kept = Object.entries(current).filter(([id]) => keep(Number(id)));
  if (kept.length === Object.keys(current).length) return all;
  if (kept.length === 0) return withoutAgent(all, agentId);
  return { ...all, [agentId]: Object.fromEntries(kept) };
}

export function withoutQuestions(all: QuestionDrafts, agentId: number, ids: readonly number[]): QuestionDrafts {
  return keeping(all, agentId, (id) => !ids.includes(id));
}

export function onlyOpen(all: QuestionDrafts, agentId: number, openIds: readonly number[]): QuestionDrafts {
  return keeping(all, agentId, (id) => openIds.includes(id));
}
