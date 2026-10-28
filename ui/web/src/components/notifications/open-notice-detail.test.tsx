// OpenNoticeDetail — the shared open-notice reply surface used by the inspector
// (and, after the queue-merge cut, the fleet queue). Verifies the resolve calls
// per notice kind, the empty-reply guard, the 409 message, and onResolved.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, fireEvent, screen, waitFor } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { afterEach, describe, expect, it, vi, beforeEach } from "vitest";

import { OpenNoticeDetail } from "./open-notice-detail";
import { pressEnter } from "@/test-support/enter-key";
import { formatAbsolute, formatRelative } from "@/lib/format/time";
import { USER_SETTING_DEFAULTS, type OpenNotice } from "@/lib/contracts/types";
import { SETTINGS_QUERY_KEY } from "@/lib/state/use-user-settings";

import { resetTransport, transport } from "@/test-support/fake-api";
beforeEach(() => {
  resetTransport();
  transport.resolveNotice.mockImplementation(resolveNotice);
  transport.getSettings.mockImplementation(() => Promise.resolve({ settings: [] }));
});

const { resolveNotice } = vi.hoisted(() => ({
  resolveNotice:
    vi.fn<(agentId: number, noticeId: number, body: unknown, key?: string) => Promise<{ status: string }>>(),
}));

function renderNotice(ui: React.ReactElement) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  qc.setQueryData(SETTINGS_QUERY_KEY, { ...USER_SETTING_DEFAULTS });
  return renderWithBrowser(<QueryClientProvider client={qc}>{ui}</QueryClientProvider>);
}

function ntc(overrides: Partial<OpenNotice> = {}): OpenNotice {
  return {
    id: 11,
    title: "Approve deploy?",
    content: "push to prod?",
    priority: "P0",
    require_response: true,
    blocking: false,
    created_at: "2026-06-14T12:00:00Z",
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  resolveNotice.mockReset();
  vi.restoreAllMocks();
});

// meiva:note
// The one text field the current notice renders — the answer box of a question,
// or the reply box of a notice without one.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md
function field(): HTMLTextAreaElement {
  return screen.getByRole<HTMLTextAreaElement>("textbox");
}

describe("OpenNoticeDetail — timestamp", () => {
  const createdAt = "2026-06-14T12:00:00Z";
  const timestamp = `${formatRelative(createdAt, new Date())}, ${formatAbsolute(createdAt)}`;

  it("hides the created time by default", () => {
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ created_at: createdAt })} />);

    expect(screen.queryByText(timestamp)).toBeNull();
  });

  it("shows the created time when requested", () => {
    renderNotice(
      <OpenNoticeDetail
        agentId={7}
        notice={ntc({ created_at: createdAt })}
        showTimestamp
      />,
    );

    expect(screen.getByText(timestamp)).toBeTruthy();
  });

  it("shows expiry time when expire_at is present", () => {
    const expireAt = "2026-06-15T12:00:00Z";
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ expire_at: expireAt })} />);
    expect(screen.getByText(new RegExp(formatRelative(expireAt, new Date())))).toBeTruthy();
  });
});

describe("OpenNoticeDetail — require_response", () => {
  it("uses the same monospace input typography as the page composer", () => {
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc()} />);

    expect(screen.getByRole<HTMLTextAreaElement>("textbox").className).toContain("font-mono");
  });

  it("answer: sends action=answer with the typed reply, then calls onResolved", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    const onResolved = vi.fn();
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc()} onResolved={onResolved} />);

    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "ship it" } });
    fireEvent.click(screen.getByLabelText("Send answer"));

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", reply: "ship it" }, expect.any(String)),
    );
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
  });

  it("send stays disabled until a non-empty reply is typed", () => {
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc()} />);
    const send = screen.getByLabelText("Send answer");
    expect(send.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "x" } });
    expect(send.hasAttribute("disabled")).toBe(false);
  });

  it("dismiss: sends action=dismiss with no reply, then calls onResolved", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    const onResolved = vi.fn();
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc()} onResolved={onResolved} />);

    fireEvent.click(screen.getByRole("button", { name: "Close without answering" }));

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "dismiss" }, expect.any(String)),
    );
    await waitFor(() => expect(onResolved).toHaveBeenCalled());
  });

  it("surfaces a friendly message on a 409 (already resolved)", async () => {
    resolveNotice.mockRejectedValue(new Error("HTTP 409 conflict"));
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc()} />);

    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "x" } });
    fireEvent.click(screen.getByLabelText("Send answer"));

    await waitFor(() =>
      expect(screen.getByText("This notice was already answered.")).toBeTruthy(),
    );
  });
});

describe("OpenNoticeDetail — reply retries", () => {
  it("reuses the original read-reply key on a manual retry after response loss", async () => {
    resolveNotice.mockRejectedValueOnce(new TypeError("response lost"))
      .mockResolvedValueOnce({ status: "idling" });
    const onResolved = vi.fn();
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc()} onResolved={onResolved} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "noted" } });
    fireEvent.click(screen.getByLabelText("Send answer"));
    await waitFor(() => expect(screen.getByText(/response lost/)).toBeTruthy());
    expect(resolveNotice).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByLabelText("Send answer"));
    await waitFor(() => expect(onResolved).toHaveBeenCalledTimes(1));
    expect(resolveNotice.mock.calls[1]).toEqual(resolveNotice.mock.calls[0]);
    expect(resolveNotice.mock.calls[0][3]).toEqual(expect.any(String));
  });

  it("allocates a new key when the reply changes after a failed submission", async () => {
    resolveNotice.mockRejectedValueOnce(new TypeError("response lost"))
      .mockResolvedValueOnce({ status: "idling" });
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc()} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "first" } });
    fireEvent.click(screen.getByLabelText("Send answer"));
    await waitFor(() => expect(screen.getByText(/response lost/)).toBeTruthy());
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "second" } });
    fireEvent.click(screen.getByLabelText("Send answer"));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledTimes(2));
    expect(resolveNotice.mock.calls[1][3]).not.toBe(resolveNotice.mock.calls[0][3]);
    expect(resolveNotice.mock.calls[1][2]).toEqual({ action: "answer", reply: "second" });
  });

  it("sends one request while repeated Enter events arrive during a pending reply", () => {
    resolveNotice.mockImplementation(() => new Promise(() => {
      // meiva:note
      // Keep the request unresolved while repeated keyboard events arrive.
    }));
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc()} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "noted" } });
    fireEvent.keyDown(screen.getByRole<HTMLTextAreaElement>("textbox"), { key: "Enter" });
    fireEvent.keyDown(screen.getByRole<HTMLTextAreaElement>("textbox"), { key: "Enter" });
    expect(resolveNotice).toHaveBeenCalledTimes(1);
  });

  it("has no mark-read action or FYI label", () => {
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc()} />);
    expect(screen.queryByText("Mark read")).toBeNull();
    expect(screen.queryByText("FYI")).toBeNull();
  });
});

const options = [{ id: "web", label: "Web" }, { id: "desktop", label: "Desktop" }];

describe("OpenNoticeDetail — numbered questions", () => {
  it.each(["single_select", "multi_select"] as const)("renders %s notice Markdown while preserving selection and comment submission", async (type) => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    const { container } = renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({
      title: "Choose **coverage** for `preview`",
      content: "Review **scope**:\n\n- Conversation\n- Questions",
      question: { type, options: [{ id: "web", label: "**Web** `app`", description: "Use [docs](https://example.com/docs) and *keyboard*." }], allow_other: false },
    })} />);
    await waitFor(() => expect(screen.getByRole("heading").querySelector("strong")?.textContent).toBe("coverage"));
    expect(screen.getByRole("heading").querySelector("code")?.textContent).toBe("preview");
    expect(screen.getByRole("list").textContent).toContain("Conversation");
    const choice = screen.getByRole(type === "single_select" ? "radio" : "checkbox", { name: /^1\. Web app Use docs and keyboard\s*\.$/ });
    expect(choice.closest("label")?.querySelector("strong")?.textContent).toBe("Web");
    expect(choice.closest("label")?.querySelector("em")?.textContent).toBe("keyboard");
    expect(container.querySelector("label div, label p, h4 div, h4 p")).toBeNull();
    expect(screen.getByRole("link", { name: "docs" }).getAttribute("href")).toBe("https://example.com/docs");
    fireEvent.click(choice);
    fireEvent.change(screen.getByRole("textbox", { name: "Comment (optional)" }), { target: { value: "Keep **this** literal." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 11, {
      action: "answer", answer: { option_ids: ["web"], comment: "Keep **this** literal." },
    }, expect.any(String)));
  });

  it("does not preselect or submit a single choice until explicit confirmation", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "single_select", options, allow_other: false } })} />);
    const submit = screen.getByRole("button", { name: "Submit answer" });
    expect(submit.hasAttribute("disabled")).toBe(true);
    fireEvent.click(screen.getByRole<HTMLInputElement>("radio", { name: "1. Web" }));
    fireEvent.click(screen.getByRole<HTMLInputElement>("radio", { name: "2. Desktop" }));
    expect((screen.getByRole<HTMLInputElement>("radio", { name: "1. Web" })).checked).toBe(false);
    expect(resolveNotice).not.toHaveBeenCalled();
    fireEvent.click(submit);
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", answer: { option_ids: ["desktop"] } }, expect.any(String)));
  });

  it.each(["single_select", "multi_select"] as const)("%s submits a comment-only answer when nothing is chosen", async (type) => {
    // meiva:note
    // Choosing nothing is allowed: a comment alone is a whole answer (user
    // 2026-10-21), so the submit button leaves its disabled state on the comment
    // alone and the payload carries an empty selection.
    // meiva:link meiva_builtins/plugins/meiva_fleet/docs/notice-questions.meiva.okf.md
    resolveNotice.mockResolvedValue({ status: "ok" });
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type, options, allow_other: false } })} />);
    const submit = screen.getByRole("button", { name: "Submit answer" });
    expect(submit.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByRole("textbox", { name: "Comment (optional)" }), { target: { value: "Neither fits — hold until Monday." } });
    expect(submit.hasAttribute("disabled")).toBe(false);
    fireEvent.click(submit);
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 11, {
      action: "answer", answer: { option_ids: [], comment: "Neither fits — hold until Monday." },
    }, expect.any(String)));
  });

  it("lets a single choice be cleared by choosing it again", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "single_select", options, allow_other: false } })} />);
    const first = screen.getByRole<HTMLInputElement>("radio", { name: "1. Web" });
    const submit = screen.getByRole("button", { name: "Submit answer" });
    fireEvent.click(first);
    expect(first.checked).toBe(true);
    fireEvent.click(first);
    expect(first.checked).toBe(false);
    expect(submit.hasAttribute("disabled")).toBe(true);
    fireEvent.change(screen.getByRole("textbox", { name: "Comment (optional)" }), { target: { value: "Hold one more day." } });
    fireEvent.click(submit);
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 11, {
      action: "answer", answer: { option_ids: [], comment: "Hold one more day." },
    }, expect.any(String)));
  });

  it.each(["single_select", "multi_select"] as const)("%s preserves comments independently of selections", async (type) => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type, options, allow_other: false } })} />);
    const comment = screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Comment (optional)" });
    fireEvent.change(comment, { target: { value: "Based on 2, defer 1." } });
    const submit = screen.getByRole("button", { name: "Submit answer" });
    const role = type === "single_select" ? "radio" : "checkbox";
    fireEvent.click(screen.getByRole(role, { name: "1. Web" }));
    fireEvent.click(screen.getByRole(role, { name: "2. Desktop" }));
    expect(comment.value).toBe("Based on 2, defer 1.");
    expect(screen.queryByRole(role, { name: /Other/ })).toBeNull();
    expect(screen.queryByRole("button", { name: "Add a note" })).toBeNull();
    fireEvent.click(submit);
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 11, {
      action: "answer", answer: {
        option_ids: type === "single_select" ? ["desktop"] : ["web", "desktop"],
        comment: "Based on 2, defer 1.",
      },
    }, expect.any(String)));
    expect(comment.disabled).toBe(true);
  });

  it("open questions keep their multiline text when Enter sends them", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "text" } })} />);
    const box = screen.getByRole<HTMLTextAreaElement>("textbox");
    fireEvent.change(box, { target: { value: "Use 2.\nThen 1." } });
    pressEnter(box);
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", answer: { text: "Use 2.\nThen 1." } }, expect.any(String)));
  });

  it("retains a failed answer and reuses its operation key on a manual retry", async () => {
    resolveNotice.mockRejectedValueOnce(new TypeError("response lost")).mockResolvedValueOnce({ status: "ok" });
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "text" } })} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "Keep it readable." } });
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("response lost"));
    expect((screen.getByRole<HTMLTextAreaElement>("textbox")).value).toBe("Keep it readable.");
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(resolveNotice).toHaveBeenCalledTimes(2));
    expect(resolveNotice.mock.calls[1]).toEqual(resolveNotice.mock.calls[0]);
  });

  it("disables stale questions after a conflict", async () => {
    resolveNotice.mockRejectedValue(new Error("HTTP 409 expired"));
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "text" } })} />);
    fireEvent.change(screen.getByRole<HTMLTextAreaElement>("textbox"), { target: { value: "Yes" } });
    fireEvent.click(screen.getByRole("button", { name: "Submit answer" }));
    await waitFor(() => expect(screen.getByRole("alert").textContent).toContain("no longer available"));
    expect(screen.getByRole("button", { name: "Submit answer" }).hasAttribute("disabled")).toBe(true);
  });
});

// meiva:note
// The reported bug (user, 2026-10-28): replying to a notification typed a newline
// instead of sending. Every reply field now shares one rule: Enter sends,
// Shift+Enter breaks the line at the caret, an input method keeps its keys, and an
// incomplete answer sends nothing.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md
describe("OpenNoticeDetail — Enter sends", () => {
  it("Enter in a question's answer box sends the answer", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "text" } })} />);
    fireEvent.change(field(), { target: { value: "Use 2." } });

    pressEnter(field());

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", answer: { text: "Use 2." } }, expect.any(String)),
    );
  });

  it("Enter in the free-text reply box sends the reply", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc()} />);
    fireEvent.change(field(), { target: { value: "ship it" } });

    pressEnter(field());

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 11, { action: "answer", reply: "ship it" }, expect.any(String)),
    );
  });

  it("Enter in the optional comment of a question sends the answer with it", async () => {
    resolveNotice.mockResolvedValue({ status: "ok" });
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "single_select", options: [{ id: "a", label: "A", description: null }], allow_other: false } })} />);
    fireEvent.change(field(), { target: { value: "second choice is fine too" } });

    pressEnter(field());

    await waitFor(() =>
      expect(resolveNotice).toHaveBeenCalledWith(7, 11, {
        action: "answer",
        answer: { option_ids: [], comment: "second choice is fine too" },
      }, expect.any(String)),
    );
  });

  it("Shift+Enter breaks the line at the caret and sends nothing", () => {
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "text" } })} />);
    fireEvent.change(field(), { target: { value: "firstsecond" } });
    field().setSelectionRange(5, 5);

    pressEnter(field(), { shiftKey: true });

    expect(field().value).toBe("first\nsecond");
    expect(field().selectionStart).toBe(6);
    expect(resolveNotice).not.toHaveBeenCalled();
  });

  it("Enter that commits an IME candidate neither sends nor loses the key", () => {
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "text" } })} />);
    fireEvent.change(field(), { target: { value: "\u4f60\u597d" } });

    expect(fireEvent.keyDown(field(), { key: "Enter", isComposing: true })).toBe(true);

    expect(resolveNotice).not.toHaveBeenCalled();
    expect(field().value).toBe("\u4f60\u597d");
  });

  it("Enter in a blank or whitespace-only answer sends nothing", () => {
    renderNotice(<OpenNoticeDetail agentId={7} notice={ntc({ question: { type: "text" } })} />);
    fireEvent.change(field(), { target: { value: "   " } });

    pressEnter(field());

    expect(resolveNotice).not.toHaveBeenCalled();
  });
});
