// meiva:note
// QuestionsForm: one form for all of an agent's open questions, each answered or skipped, one
// submission, and a changed open set merges without losing what the user already filled in.

import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { QuestionsForm } from "./questions-form";
import { pressEnter } from "@/test-support/enter-key";
import type { QuestionItem } from "@/lib/contracts/types";
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

const submit = () => screen.getByRole<HTMLButtonElement>("button", { name: "Submit answers" });
const article = (n: number) => within(screen.getByRole("article", { name: `Question ${n}` }));

beforeEach(() => {
  resetTransport();
  transport.getSettings.mockResolvedValue({ settings: [] });
});
afterEach(cleanup);

describe("QuestionsForm", () => {
  it("lists the questions in id order and submits nothing until every one is answered or skipped", () => {
    renderForm([q(5), q(3)]);
    expect(screen.getAllByRole("article").map((a) => a.getAttribute("aria-label"))).toEqual(["Question 3", "Question 5"]);
    expect(submit().disabled).toBe(true);
    fireEvent.change(article(3).getByRole("textbox"), { target: { value: "x" } });
    expect(submit().disabled).toBe(true);
    fireEvent.click(article(5).getByRole("button", { name: "Skip" }));
    expect(submit().disabled).toBe(false);
    fireEvent.click(article(5).getByRole("button", { name: "Undo skip" }));
    expect(submit().disabled).toBe(true);
  });

  it("sends one submission with a choice, a comment-only answer, a text answer and a skip", async () => {
    transport.answerQuestions.mockResolvedValue({ kind: "settled" });
    const onSettled = vi.fn();
    renderForm([q(1, { question: choice("single_select") }), q(2, { question: choice("multi_select") }), q(3), q(4)], { onSettled });
    fireEvent.click(article(1).getByRole("radio", { name: /Web/ }));
    fireEvent.change(article(2).getByRole("textbox"), { target: { value: "none of these" } });
    fireEvent.change(article(3).getByRole("textbox"), { target: { value: "because" } });
    fireEvent.click(article(4).getByRole("button", { name: "Skip" }));
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
    rerender(<QuestionsForm agentId={7} questions={[q(1), q(2)]} onSettled={onSettled} onChanged={vi.fn()} />);
    expect(article(2).getByRole<HTMLTextAreaElement>("textbox").disabled).toBe(false);
    expect(article(1).getByRole<HTMLTextAreaElement>("textbox").disabled).toBe(false);
    fireEvent.change(article(2).getByRole("textbox"), { target: { value: "y" } });
    expect(submit().disabled).toBe(false);
  });

  it("keeps a skipped question's draft when the skip is undone", () => {
    renderForm([q(1)]);
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "draft" } });
    fireEvent.click(article(1).getByRole("button", { name: "Skip" }));
    fireEvent.click(article(1).getByRole("button", { name: "Undo skip" }));
    expect(article(1).getByRole<HTMLTextAreaElement>("textbox").value).toBe("draft");
    expect(submit().disabled).toBe(false);
  });

  it("sends from Enter in an answer field only when the whole form is complete", async () => {
    transport.answerQuestions.mockResolvedValue({ kind: "settled" });
    renderForm([q(1), q(2)]);
    fireEvent.change(article(1).getByRole("textbox"), { target: { value: "one" } });
    pressEnter(article(1).getByRole("textbox"));
    expect(transport.answerQuestions).not.toHaveBeenCalled();
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
    fireEvent.click(article(2).getByRole("button", { name: "Skip" }));
    fireEvent.click(submit());
    await waitFor(() => expect(onChanged).toHaveBeenCalledWith(latest));
    expect(screen.getByRole("status").textContent).toBe("The questions changed. Review them and submit again.");
    rerender(<QuestionsForm agentId={7} questions={latest} onSettled={vi.fn()} onChanged={onChanged} />);
    expect(article(1).getByRole<HTMLTextAreaElement>("textbox").value).toBe("kept");
    expect(submit().disabled).toBe(true);
    fireEvent.change(article(3).getByRole("textbox"), { target: { value: "new" } });
    expect(submit().disabled).toBe(false);
  });
});
