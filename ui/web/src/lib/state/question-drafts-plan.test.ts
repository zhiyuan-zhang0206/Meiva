import { describe, expect, it } from "vitest";

import { EMPTY_DRAFT, onlyOpen, withDraft, withoutQuestions } from "./question-drafts-plan";

const typed = { ...EMPTY_DRAFT, text: "x" };

describe("question drafts", () => {
  it("keeps one agent's drafts apart from another's", () => {
    const all = withDraft(withDraft({}, 7, 1, typed), 9, 2, { ...EMPTY_DRAFT, comment: "note" });
    expect(all[7]?.[1]).toBe(typed);
    expect(all[9]?.[2]?.comment).toBe("note");
  });

  it("clears answered questions and drops an agent left with none", () => {
    const all = withDraft(withDraft(withDraft({}, 7, 1, typed), 7, 2, typed), 9, 3, typed);
    const some = withoutQuestions(all, 7, [1]);
    expect(Object.keys(some[7] ?? {})).toEqual(["2"]);
    expect(some[9]).toBe(all[9]);
    expect(withoutQuestions(some, 7, [2])[7]).toBeUndefined();
  });

  it("keeps only the open questions of one agent and returns the same object when nothing goes", () => {
    const all = withDraft(withDraft({}, 7, 1, typed), 7, 2, typed);
    expect(Object.keys(onlyOpen(all, 7, [2, 5])[7] ?? {})).toEqual(["2"]);
    expect(onlyOpen(all, 7, [1, 2])).toBe(all);
    expect(onlyOpen(all, 3, [])).toBe(all);
    expect(withoutQuestions(all, 7, [99])).toBe(all);
  });
});
