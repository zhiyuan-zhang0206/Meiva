import { cleanup, fireEvent, screen, waitFor, within } from "@testing-library/react";
import type { ReactElement } from "react";
import { afterEach, beforeEach, expect, it } from "vitest";

import type { NoticeItem } from "@/lib/contracts/types";
import { QUESTION_REGION_MAX_HEIGHT_CLASS } from "@/lib/layout/layout";
import { NOTICES_QUERY_KEY, dropTombstonesResetForTest } from "@/lib/notifications/use-notices";
import { resetTransport, transport } from "@/test-support/fake-api";
import { renderWithProviders } from "@/test-support/render";
import { PendingQuestions } from "./pending-questions";

const questions: NoticeItem[] = [1, 2].map((id) => ({
  id, agent_id: 7, agent_label: null, title: `Question ${id}`, content: null,
  priority: "P1", require_response: true, blocking: false,
  created_at: "2026-11-07T00:00:00Z", expire_at: null, resolved_at: null,
  question: { type: "text", note_suggestion: `Suggested answer ${id}` },
}));

function renderQuestions(ui: ReactElement) {
  return renderWithProviders(ui, { wallNow: () => Date.parse("2026-11-07T00:00:00Z") });
}

beforeEach(() => {
  resetTransport();
  transport.getSettings.mockResolvedValue({ settings: [] });
});
afterEach(() => { cleanup(); dropTombstonesResetForTest(); });

it("anchors a collapsed queue above the composer and retains independent drafts through collapse", () => {
  renderQuestions(<PendingQuestions questions={questions} failed={false} maxWidthCss="768px" />);
  const region = screen.getByTestId("pending-questions");
  expect(region.className).toContain("absolute");
  expect(region.className).toContain("bottom-full");
  expect(region.style.maxWidth).toBe("768px");
  const toggle = screen.getByRole("button", { name: "2 questions waiting" });
  expect(toggle.getAttribute("aria-expanded")).toBe("false");
  expect(screen.queryByRole("textbox")).toBeNull();
  fireEvent.click(toggle);
  const first = within(screen.getByRole("article", { name: "Question 1" })).getByRole<HTMLTextAreaElement>("textbox");
  const second = within(screen.getByRole("article", { name: "Question 2" })).getByRole<HTMLTextAreaElement>("textbox");
  expect(first.value).toBe("");
  expect(second.value).toBe("");
  fireEvent.change(first, { target: { value: "First answer" } });
  fireEvent.change(second, { target: { value: "Second answer" } });
  const viewport = region.querySelector('[data-slot="scroll-area-viewport"]')!;
  expect(viewport.className).toContain(QUESTION_REGION_MAX_HEIGHT_CLASS);
  fireEvent.click(toggle);
  expect(screen.queryByRole("textbox")).toBeNull();
  fireEvent.click(toggle);
  expect(first.value).toBe("First answer");
  expect(second.value).toBe("Second answer");
  expect(screen.getAllByRole("textbox")).toHaveLength(2);
});

it("resolves only the selected question and drops it from the shared pending queue", async () => {
  transport.resolveNotice.mockResolvedValue({ status: "ok" });
  const { queryClient } = renderQuestions(<PendingQuestions questions={questions} failed={false} />);
  queryClient.setQueryData(NOTICES_QUERY_KEY, { awaiting: questions, open: [], resolved_page: [], next_cursor: null });
  fireEvent.click(screen.getByRole("button", { name: "2 questions waiting" }));
  const first = within(screen.getByRole("article", { name: "Question 1" }));
  fireEvent.change(first.getByRole("textbox"), { target: { value: "First answer" } });
  fireEvent.click(first.getByRole("button", { name: "Submit answer" }));
  await waitFor(() => expect(transport.resolveNotice).toHaveBeenCalledWith(7, 1, { action: "answer", answer: { text: "First answer" } }, expect.any(String)));
  await waitFor(() => expect(queryClient.getQueryData<{ awaiting: NoticeItem[] }>(NOTICES_QUERY_KEY)?.awaiting.map((q) => q.id)).toEqual([2]));
  expect(within(screen.getByRole("article", { name: "Question 2" })).getByRole("textbox").hasAttribute("disabled")).toBe(false);
});

it("retains drafts on a failed answer and permits retry", async () => {
  transport.resolveNotice.mockRejectedValueOnce(new Error("offline")).mockResolvedValue({ status: "ok" });
  renderQuestions(<PendingQuestions questions={[questions[0]]} failed={false} />);
  fireEvent.click(screen.getByRole("button", { name: "1 question waiting" }));
  const input = screen.getByRole<HTMLTextAreaElement>("textbox");
  fireEvent.change(input, { target: { value: "Keep this" } });
  fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(input.value).toBe("Keep this");
  fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
  await waitFor(() => expect(transport.resolveNotice).toHaveBeenCalledTimes(2));
  expect(transport.resolveNotice.mock.calls[1][3]).toBe(transport.resolveNotice.mock.calls[0][3]);
});

it("shows a failed queue without losing its stale questions, and renders nothing for a known empty queue", () => {
  const { rerender } = renderQuestions(<PendingQuestions questions={questions} failed />);
  expect(screen.getByRole("status").textContent).toBe("Could not load pending questions.");
  expect(screen.getByRole("button", { name: "2 questions waiting" })).toBeTruthy();
  rerender(<PendingQuestions questions={[]} failed={false} />);
  expect(screen.queryByTestId("pending-questions")).toBeNull();
});
