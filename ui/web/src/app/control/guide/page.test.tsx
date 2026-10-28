import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useStore } from "@/lib/state/store";

const push = vi.fn();

vi.mock("next/navigation", () => ({ useRouter: () => ({ push }) }));

import GuidePage from "./page";
import { renderWithApi as render } from "@/test-support/render";
import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
});


describe("GuidePage", () => {
  beforeEach(() => {
    vi.restoreAllMocks();
    push.mockReset();
    useStore.setState({ activeId: null, toast: null });
  });

  it("ignores Enter while the draft is pending", async () => {
    const draft = transport.draftGuide.mockReturnValue(new Promise(() => undefined));
    render(<QueryClientProvider client={new QueryClient()}><GuidePage /></QueryClientProvider>);
    const input = screen.getByPlaceholderText(/Describe an operations task/);
    fireEvent.change(input, { target: { value: "install tools" } });
    fireEvent.keyDown(input, { key: "Enter" });
    await waitFor(() => expect(screen.getByRole("button", { name: "Ask Meiva" }).hasAttribute("disabled")).toBe(true));
    fireEvent.keyDown(input, { key: "Enter" });
    expect(draft).toHaveBeenCalledOnce();
  });

  it("does not inherit automatic mutation retries", async () => {
    const draft = transport.draftGuide.mockRejectedValue(new Error("response lost"));
    const client = new QueryClient({ defaultOptions: { mutations: { retry: 3, retryDelay: 0 } } });
    render(<QueryClientProvider client={client}><GuidePage /></QueryClientProvider>);
    fireEvent.change(screen.getByPlaceholderText(/Describe an operations task/), { target: { value: "install tools" } });
    fireEvent.click(screen.getByRole("button", { name: "Ask Meiva" }));
    await waitFor(() => expect(useStore.getState().toast).toBe("Guide failed: response lost"));
    expect(draft).toHaveBeenCalledOnce();
    expect(push).not.toHaveBeenCalled();
  });

  it("disables Ask until a non-blank request is entered", () => {
    render(
      <QueryClientProvider client={new QueryClient()}>
        <GuidePage />
      </QueryClientProvider>,
    );

    const ask = screen.getByRole("button", { name: "Ask Meiva" });
    expect(ask.hasAttribute("disabled")).toBe(true);

    fireEvent.change(screen.getByPlaceholderText(/Describe an operations task/), {
      target: { value: "   " },
    });
    expect(ask.hasAttribute("disabled")).toBe(true);
  });

  it("drafts a trimmed request then selects the conversation and navigates home", async () => {
    const draftGuide = transport.draftGuide.mockResolvedValue({ agent_id: 42 });
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
        <GuidePage />
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getByPlaceholderText(/Describe an operations task/), {
      target: { value: "  install the linear MCP server  " },
    });
    fireEvent.click(screen.getByRole("button", { name: "Ask Meiva" }));

    await waitFor(() => expect(draftGuide).toHaveBeenCalledWith("install the linear MCP server"));
    await waitFor(() => expect(useStore.getState().activeId).toBe(42));
    expect(useStore.getState().toast).toBe("Meiva Guide #42 created — check its conversation for progress");
    expect(push).toHaveBeenCalledWith("/");
    expect(screen.getByPlaceholderText<HTMLInputElement>(/Describe an operations task/).value).toBe("");
  });

  it("reports a draft failure without navigating", async () => {
    transport.draftGuide.mockRejectedValue(new Error("gateway unavailable"));
    render(
      <QueryClientProvider client={new QueryClient({ defaultOptions: { mutations: { retry: false } } })}>
        <GuidePage />
      </QueryClientProvider>,
    );

    fireEvent.change(screen.getByPlaceholderText(/Describe an operations task/), {
      target: { value: "install a plugin" },
    });
    fireEvent.keyDown(screen.getByPlaceholderText(/Describe an operations task/), { key: "Enter" });

    await waitFor(() => expect(useStore.getState().toast).toBe("Guide failed: gateway unavailable"));
    expect(push).not.toHaveBeenCalled();
  });
});
