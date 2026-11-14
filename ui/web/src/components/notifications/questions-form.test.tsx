// meiva:note
// QuestionsForm: one form for all of an agent's open questions, each answered or skipped, one
// submission, and a changed open set merges without losing what the user already filled in.

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QuestionsForm } from "./questions-form";
import { pressEnter } from "@/test-support/enter-key";
import type { QuestionItem } from "@/lib/contracts/types";
import { useStore } from "@/lib/state/store";
import { resetTransport, transport } from "@/test-support/fake-api";
import { renderWithProviders } from "@/test-support/render";

function q(id: number, overrides: Partial<QuestionItem> = {}): QuestionItem {
  return {
    id, agent_id: 7, agent_label: null, title: `Question ${id}`, content: null,
    created_at: "2026-11-07T00:00:00Z", resolved_at: null, question: { type: "text" },
    ...overrides,
  };
}

const choice = (type: "single_select" | "multi_select") => ({
  type, options: [{ id: "web", label: "Web" }, { id: "cli", label: "CLI" }],
}) as QuestionItem["question"];

function renderForm(questions: QuestionItem[], handlers: { onSettled?: (ids: number[]) => void; onChanged?: (open: QuestionItem[]) => void } = {}) {
  return renderWithProviders(
    <QuestionsForm agentId={7} questions={questions} onSettled={handlers.onSettled ?? vi.fn()} onChanged={handlers.onChanged ?? vi.fn()} />,
  );
}

const next = () => fireEvent.click(screen.getByRole("button", { name: "Next question" }));
const previous = () => fireEvent.click(screen.getByRole("button", { name: "Previous question" }));
const submit = () => screen.getByRole<HTMLButtonElement>("button", { name: "Submit answers" });
const article = (n: number) => within(screen.getByRole("article", { name: `Question ${n}` }));

beforeEach(() => {
  useStore.setState({ questionDrafts: {} });
  resetTransport();
  transport.getSettings.mockResolvedValue({ settings: [] });
});
afterEach(cleanup);

describe("QuestionsForm", () => {
  it("shows one question at a time in id order and keeps drafts while navigating", () => {
    renderForm([q(5), q(3)]);
    expect(screen.getAllByRole("article")).toHaveLength(1);
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Previous question" }).disabled).toBe(true);
    expect(screen.getByText("1 / 2")).toBeTruthy();
    expect(submit().disabled).toBe(true);
    fireEvent.change(article(3).getByRole("textbox"), { target: { value: "x" } });
    expect(submit().disabled).toBe(true);
    next();
    expect(screen.queryByRole("article", { name: "Question 3" })).toBeNull();
    expect(screen.getByRole<HTMLButtonElement>("button", { name: "Next question" }).disabled).toBe(true);
    expect(screen.getByText("2 / 2")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(submit().disabled).toBe(false);
    previous();
    expect(article(3).getByRole<HTMLTextAreaElement>("textbox").value).toBe("x");
    next();
    fireEvent.click(screen.getByRole("button", { name: "Undo skip" }));
    expect(submit().disabled).toBe(true);
    expect(transport.answerQuestions).not.toHaveBeenCalled();
  });

  it("sends one submission with a choice, a comment-only answer, a text answer and a skip", async () => {
    transport.answerQuestions.mockResolvedValue({ kind: "settled" });
    const onSettled = vi.fn();
    renderForm([q(1, { question: choice("single_select") }), q(2, { question: choice("multi_select") }), q(3), q(4)], { onSettled });
    fireEvent.click(article(1).getByRole("radio", { name: /Web/ }));
    next();
    fireEvent.change(article(2).getByRole("textbox"), { target: { value: "none of these" } });
    next();
    fireEvent.change(article(3).getByRole("textbox"), { target: { value: "because" } });
    next();
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    fireEvent.click(submit());
    await waitFor(() => expect(onSettled).toHaveBeenCalledWith([1, 2, 3, 4]));
    expect(transport.answerQuestions).toHaveBeenCalledTimes(1);
    expect(transport.answerQuestions.mock.calls[0][0]).toBe(7);
    expect(transport.answerQuestions.mock.calls[0][1]).toEqual({
      expected_ids: [1, 2, 3, 4],
      results: [
        { id: 1, answer: { option_ids: ["web"] }, skip: false },
        { id: 2, answer: { option_ids: [], comment: "none of these" }, skip: false },
        { id: 3, answer: { text: "because" }, skip: false },
        { id: 4, skip: true },
      ],
    });
  });

  it("is usable again after a successful submission while the form stays mounted (a new question arrived)", async () => {
    transport.answerQuestions.mockResolvedValue({ kind: "settled" });
    const onSettled = vi.fn();
    const { rerender } = renderForm([q(1)], { onSettled });
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "x" } });
    fireEvent.click(submit());
    await waitFor(() => expect(onSettled).toHaveBeenCalledWith([1]));
    rerender(<QuestionsForm agentId={7} questions={[q(2)]} onSettled={onSettled} onChanged={vi.fn()} />);
    expect(article(2).getByRole<HTMLTextAreaElement>("textbox").disabled).toBe(false);
    fireEvent.change(article(2).getByRole("textbox"), { target: { value: "y" } });
    expect(submit().disabled).toBe(false);
  });

  it("skips and advances, then restores the draft in place when the skip is undone", () => {
    renderForm([q(1), q(2)]);
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "draft" } });
    const skip = screen.getByRole("button", { name: "Skip" });
    fireEvent.click(skip);
    expect(screen.getByText("2 / 2")).toBeTruthy();
    expect(screen.getByRole("button", { name: "Skip" })).toBe(skip);
    expect(submit().disabled).toBe(true);
    previous();
    expect(screen.getByText("Skipped")).toBeTruthy();
    expect(article(1).queryByRole("textbox")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Undo skip" }));
    expect(screen.getByText("1 / 2")).toBeTruthy();
    expect(article(1).getByRole<HTMLTextAreaElement>("textbox").value).toBe("draft");
    next();
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    expect(screen.getByText("2 / 2")).toBeTruthy();
    expect(submit().disabled).toBe(false);
    expect(transport.answerQuestions).not.toHaveBeenCalled();
  });

  it("sends from Enter in an answer field only when the whole form is complete", async () => {
    transport.answerQuestions.mockResolvedValue({ kind: "settled" });
    renderForm([q(1), q(2)]);
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "one" } });
    pressEnter(article(1).getByRole("textbox"));
    expect(transport.answerQuestions).not.toHaveBeenCalled();
    next();
    fireEvent.change(article(2).getByRole("textbox"), { target: { value: "two" } });
    pressEnter(article(2).getByRole("textbox"));
    await waitFor(() => expect(transport.answerQuestions).toHaveBeenCalledTimes(1));
  });

  it("disables the controls only while a submission is in flight", async () => {
    transport.answerQuestions.mockImplementation(() => new Promise<never>(() => undefined));
    renderForm([q(1)]);
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "x" } });
    fireEvent.click(submit());
    await waitFor(() => expect(article(1).getByRole<HTMLTextAreaElement>("textbox").disabled).toBe(true));
    fireEvent.click(submit());
    expect(transport.answerQuestions).toHaveBeenCalledTimes(1);
  });

  it("reports a failure, keeps every draft and retries the same intent under the same key", async () => {
    transport.answerQuestions.mockRejectedValueOnce(new TypeError("response lost")).mockResolvedValue({ kind: "settled" });
    renderForm([q(1)]);
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "x" } });
    fireEvent.click(submit());
    expect((await screen.findByRole("alert")).textContent).toContain("Submit answers failed");
    expect(article(1).getByRole<HTMLTextAreaElement>("textbox").value).toBe("x");
    fireEvent.click(submit());
    await waitFor(() => expect(transport.answerQuestions).toHaveBeenCalledTimes(2));
    expect(transport.answerQuestions.mock.calls[1]).toEqual(transport.answerQuestions.mock.calls[0]);
  });

  it("takes the latest open set when the server's differs, states it as a fact, and keeps the drafts of the questions still open", async () => {
    const latest = [q(1), q(3)];
    transport.answerQuestions.mockResolvedValue({ kind: "changed", open: latest });
    const onChanged = vi.fn();
    const { rerender } = renderForm([q(1), q(2)], { onChanged });
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "kept" } });
    next();
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    fireEvent.click(submit());
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(latest));
    expect(screen.getByRole("status").textContent).toBe("The questions changed. Review them and submit again.");
    rerender(<QuestionsForm agentId={7} questions={latest} onSettled={vi.fn()} onChanged={onChanged} />);
    expect(article(1).getByRole<HTMLTextAreaElement>("textbox").value).toBe("kept");
    expect(submit().disabled).toBe(true);
    next();
    fireEvent.change(article(3).getByRole("textbox"), { target: { value: "new" } });
    expect(submit().disabled).toBe(false);
  });

  it("keeps drafts and skips when the user goes to another agent and comes back", () => {
    const first = renderForm([q(1), q(2)]);
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "kept" } });
    next();
    fireEvent.click(screen.getByRole("button", { name: "Skip" }));
    first.unmount();
    renderForm([q(1), q(2)]);
    expect(article(1).getByRole<HTMLTextAreaElement>("textbox").value).toBe("kept");
    next();
    expect(screen.getByRole("button", { name: "Undo skip" })).toBeTruthy();
    expect(submit().disabled).toBe(false);
  });

  it("does not show one agent's drafts to another", () => {
    const first = renderForm([q(1)]);
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "mine" } });
    first.unmount();
    renderWithProviders(
      <QuestionsForm agentId={9} questions={[q(1, { agent_id: 9 })]} onSettled={vi.fn()} onChanged={vi.fn()} />,
    );
    expect(article(1).getByRole<HTMLTextAreaElement>("textbox").value).toBe("");
  });

  it("drops the drafts of questions that are no longer open and of the ones just answered", async () => {
    transport.answerQuestions.mockResolvedValue({ kind: "settled" });
    const { rerender } = renderForm([q(1), q(2)]);
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "a" } });
    next();
    fireEvent.change(article(2).getByRole("textbox"), { target: { value: "b" } });
    expect(Object.keys(useStore.getState().questionDrafts[7] ?? {})).toEqual(["1", "2"]);
    rerender(<QuestionsForm agentId={7} questions={[q(2)]} onSettled={vi.fn()} onChanged={vi.fn()} />);
    await waitFor(() => expect(Object.keys(useStore.getState().questionDrafts[7] ?? {})).toEqual(["2"]));
    fireEvent.click(submit());
    await waitFor(() => expect(useStore.getState().questionDrafts[7]).toBeUndefined());
  });
});
