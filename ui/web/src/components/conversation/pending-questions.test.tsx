import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, expect, it } from "vitest";

import type { QuestionItem } from "@/lib/contracts/types";
import { QUESTION_REGION_MAX_HEIGHT_CLASS } from "@/lib/layout/layout";
import { QUESTIONS_QUERY_KEY, dropTombstonesResetForTest } from "@/lib/notifications/use-questions";
import { useStore } from "@/lib/state/store";
import { resetTransport, transport } from "@/test-support/fake-api";
import { renderWithProviders } from "@/test-support/render";
import { PendingQuestions } from "./pending-questions";

const questions: QuestionItem[] = [1, 2].map((id) => ({
  id, agent_id: 7, agent_label: null, title: `Question ${id}`, content: null,
  created_at: "2026-11-07T00:00:00Z", resolved_at: null,
  question: { type: "text", note_suggestion: `Suggested answer ${id}` },
}));

function renderQuestions(ui: ReactElement) {
  return renderWithProviders(ui, { wallNow: () => Date.parse("2026-11-07T00:00:00Z") });
}

beforeEach(() => {
  useStore.setState({ questionDrafts: {}, questionPanelCollapsed: {} });
  resetTransport();
  transport.getSettings.mockResolvedValue({ settings: [] });
});
afterEach(() => { cleanup(); dropTombstonesResetForTest(); });

it("opens expanded by default above the composer and retains independent drafts through collapse", () => {
  renderQuestions(<PendingQuestions agentId={7} questions={questions} failed={false} maxWidthCss="768px" />);
  const region = screen.getByTestId("pending-questions");
  expect(region.className).toContain("relative");
  expect(region.style.maxWidth).toBe("768px");
  const toggle = screen.getByRole("button", { name: "2 questions waiting" });
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  const answerLayer = document.getElementById(toggle.getAttribute("aria-controls")!)!;
  expect(answerLayer.parentElement?.className).toContain("absolute");
  expect(answerLayer.parentElement?.className).toContain("bottom-1");
  expect(answerLayer.parentElement).toBe(toggle.parentElement);
  const input = () => screen.getByRole<HTMLTextAreaElement>("textbox");
  expect(input().value).toBe("");
  fireEvent.change(input(), { target: { value: "First answer" } });
  fireEvent.click(screen.getByRole("button", { name: "Next question" }));
  expect(input().value).toBe("");
  fireEvent.change(input(), { target: { value: "Second answer" } });
  expect(screen.queryByText("2 questions waiting")).toBeNull();
  const viewport = input().closest('[data-slot="scroll-area-viewport"]')!;
  expect(viewport.className).toContain(QUESTION_REGION_MAX_HEIGHT_CLASS);
  expect(viewport.contains(screen.getByRole("button", { name: "Submit" }))).toBe(false);
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.getByText("2 questions waiting")).toBeTruthy();
  expect(screen.queryByRole("button", { name: "Submit" })).toBeNull();
  fireEvent.click(toggle);
  expect(toggle.getAttribute("aria-expanded")).toBe("true");
  expect(screen.queryByText("2 questions waiting")).toBeNull();
  expect(input().value).toBe("Second answer");
  fireEvent.click(screen.getByRole("button", { name: "Previous question" }));
  expect(input().value).toBe("First answer");
  expect(screen.getAllByRole("textbox")).toHaveLength(1);
});

it("settles every open question in one submission and drops them all from the shared queue", async () => {
  transport.answerQuestions.mockResolvedValue({ kind: "settled" });
  const { queryClient } = renderQuestions(<PendingQuestions agentId={7} questions={questions} failed={false} />);
  queryClient.setQueryData(QUESTIONS_QUERY_KEY, { awaiting: questions });
  const submit = screen.getByRole<HTMLButtonElement>("button", { name: "Submit" });
  expect(submit.disabled).toBe(true);
  fireEvent.change(within(screen.getByRole("article", { name: "Question 1" })).getByRole("textbox"), { target: { value: "First answer" } });
  expect(submit.disabled).toBe(true);
  fireEvent.click(screen.getByRole("button", { name: "Next question" }));
  expect(submit.disabled).toBe(false);
  fireEvent.click(submit);
  await waitFor(() => expect(transport.answerQuestions).toHaveBeenCalledWith(7, {
    expected_ids: [1, 2],
    results: [{ id: 1, answer: { text: "First answer" } }, { id: 2 }],
  }, expect.any(String)));
  await waitFor(() => expect(queryClient.getQueryData<{ awaiting: QuestionItem[] }>(QUESTIONS_QUERY_KEY)?.awaiting).toEqual([]));
});

it("retains drafts on a failed submission and retries it under the same operation key", async () => {
  transport.answerQuestions.mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ kind: "settled" });
  renderQuestions(<PendingQuestions agentId={7} questions={[questions[0]]} failed={false} />);
  const input = screen.getByRole<HTMLTextAreaElement>("textbox");
  fireEvent.change(input, { target: { value: "Keep this" } });
  fireEvent.click(screen.getByRole("button", { name: "Submit" }));
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(input.value).toBe("Keep this");
  fireEvent.click(screen.getByRole("button", { name: "Submit" }));
  await waitFor(() => expect(transport.answerQuestions).toHaveBeenCalledTimes(2));
  expect(transport.answerQuestions.mock.calls[1][2]).toBe(transport.answerQuestions.mock.calls[0][2]);
});

it("shows a failed queue without losing its stale questions, and renders nothing for a known empty queue", () => {
  const { rerender } = renderQuestions(<PendingQuestions agentId={7} questions={questions} failed />);
  expect(screen.getByRole("status").textContent).toBe("Could not load pending questions.");
  expect(screen.getByRole("button", { name: "2 questions waiting" })).toBeTruthy();
  rerender(<PendingQuestions agentId={7} questions={[]} failed={false} />);
  expect(screen.queryByTestId("pending-questions")).toBeNull();
});

it("restores each agent's disclosure state when its question panel remounts", () => {
  const panel = (agentId: number, open = questions) => (
    <PendingQuestions key={agentId} agentId={agentId} questions={open} failed={false} />
  );
  const otherQuestions = [{ ...questions[0], id: 3, agent_id: 8 }];
  const { rerender } = renderQuestions(panel(7));
  const toggle = (label: string) => screen.getByRole("button", { name: label });
  fireEvent.click(toggle("2 questions waiting"));

  rerender(panel(8, otherQuestions));
  expect(toggle("1 question waiting").getAttribute("aria-expanded")).toBe("true");

  rerender(panel(7, []));
  expect(screen.queryByTestId("pending-questions")).toBeNull();
  rerender(panel(7));
  expect(toggle("2 questions waiting").getAttribute("aria-expanded")).toBe("false");
  fireEvent.click(toggle("2 questions waiting"));

  rerender(panel(8, otherQuestions));
  fireEvent.click(toggle("1 question waiting"));
  rerender(panel(7));
  expect(toggle("2 questions waiting").getAttribute("aria-expanded")).toBe("true");
  rerender(panel(8, otherQuestions));
  expect(toggle("1 question waiting").getAttribute("aria-expanded")).toBe("false");
});
