import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it } from "vitest";
import type { UserMessage } from "@/lib/contracts/types";
import { renderWithBrowser } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
import { MessageActions } from "./message-actions";

const question: UserMessage = {
  id: 12, agent_id: 7, mode: "sdk", source_key: "sdk:question", content: "Choose scope",
  suggestions: ["Ship", "Wait"], created_at: "2026-11-06T00:00:00Z",
  read_at: null,
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
  expect(screen.queryByText("Awaiting reply")).toBeNull();
});

it("leaves reply drafting to the composer service", () => {
  show();
  expect(screen.queryByRole("textbox")).toBeNull();
  expect(screen.queryByRole("button", { name: "Ship" })).toBeNull();
});

it("removes the unread action once a publication is read", () => {
  show({ ...question, read_at: "2026-11-06T00:01:00Z" });
  expect(screen.queryByText("Ship")).toBeNull();
  expect(screen.queryByRole("textbox")).toBeNull();
});
