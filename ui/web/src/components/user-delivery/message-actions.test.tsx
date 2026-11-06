import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import type { UserMessage } from "@/lib/contracts/types";
import { renderWithBrowser } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
import { MessageActions } from "./message-actions";

const question: UserMessage = {
  id: 12, agent_id: 7, mode: "sdk", source_key: "sdk:question", content: "Choose scope",
  response: "required", suggestions: ["Ship", "Wait"], created_at: "2026-11-06T00:00:00Z",
  read_at: null, resolved_at: null, resolution: null, reply: null,
};
beforeEach(() => {
  resetTransport();
  transport.getSettings.mockResolvedValue({ settings: { "behavior.replySuggestions": true } });
  transport.resolveUserMessage.mockResolvedValue({ status: "idling", inbound_id: null });
});
afterEach(cleanup);
function show(message: UserMessage = question) {
  const query = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  query.setQueryData(["user-settings"], { "behavior.replySuggestions": true });
  return renderWithBrowser(<QueryClientProvider client={query}><MessageActions message={message} /></QueryClientProvider>);
}

it("marks read without answering or approving", async () => {
  show();
  fireEvent.click(screen.getByRole("button", { name: "Unread · mark read" }));
  await waitFor(() => expect(transport.resolveUserMessage).toHaveBeenCalledWith(7, 12, { action: "read" }, expect.any(String)));
  expect(screen.getByText("Awaiting reply")).toBeTruthy();
});

it("makes a suggestion an editable draft and only submits the user's final text", async () => {
  show({ ...question, read_at: "2026-11-06T00:01:00Z" });
  fireEvent.click(screen.getByRole("button", { name: "Ship" }));
  expect(transport.resolveUserMessage).not.toHaveBeenCalled();
  fireEvent.change(screen.getByRole("textbox", { name: "Your reply" }), { target: { value: "Ship the smaller scope" } });
  fireEvent.click(screen.getByRole("button", { name: "Send reply" }));
  await waitFor(() => expect(transport.resolveUserMessage).toHaveBeenCalledWith(7, 12, { action: "answer", reply: "Ship the smaller scope" }, expect.any(String)));
});

it("removes stale reply actions when a message is resolved", () => {
  show({ ...question, read_at: "2026-11-06T00:01:00Z", resolved_at: "2026-11-06T00:02:00Z", resolution: "dismissed" });
  expect(screen.queryByText("Ship")).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
});
