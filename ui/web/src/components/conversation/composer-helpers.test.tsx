// meiva:note
// The composer's keydown handler and the shared Input/Textarea mobile/autofill primitives:
// 1. Keydown leaves Shift+Enter, touch keyboards, and IME composition to the textarea.
// 2. Coarse-pointer fields keep >= 16px (`pointer-coarse:text-base`) even when a caller supplies
//    `text-xs`, preventing iOS/iPadOS auto-zoom on focus.
// 3. Non-credential fields suppress Password/Credit-Card/Address autofill sheets by default, while
//    explicit credential fields (`username`, `current-password`, `new-password`, `one-time-code`)
//    keep password-manager integration intact.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md
// meiva:link ui/web/src/docs/mobile-and-tablet-ux.meiva.okf.md

import { cleanup, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { Input } from "@/components/ui/input";
import { Textarea } from "@/components/ui/textarea";
import {
  handleComposerKeyDown,
  type ComposerKeyDown,
} from "./composer-helpers";

afterEach(cleanup);

type Options = Parameters<typeof handleComposerKeyDown>[1];

function opts(overrides: Partial<Options> = {}): Options {
  return {
    ac: null,
    offeredSuggestion: null,
    composerLocked: false,
    suggestions: null,
    replyIndex: 0,
    agentId: 7,
    mode: "idle",
    coarsePointer: false,
    acceptSuggestion: () => undefined,
    onCycleSuggestion: () => undefined,
    submit: () => undefined,
    ...overrides,
  };
}

function keyDown(init: Partial<ComposerKeyDown> = {}) {
  const state = { prevented: false };
  const event: ComposerKeyDown = {
    key: "Enter",
    shiftKey: false,
    ctrlKey: false,
    altKey: false,
    metaKey: false,
    keyCode: 13,
    nativeEvent: { isComposing: false },
    preventDefault: () => {
      state.prevented = true;
    },
    ...init,
  };
  return { event, state };
}

describe("handleComposerKeyDown", () => {
  it("Enter sends on a mouse or trackpad", () => {
    const submit = vi.fn();
    const { event, state } = keyDown();
    handleComposerKeyDown(event, opts({ submit }));
    expect(submit).toHaveBeenCalledTimes(1);
    expect(state.prevented).toBe(true);
  });

  it("Shift+Enter is left to the textarea", () => {
    const submit = vi.fn();
    const { event, state } = keyDown({ shiftKey: true });
    handleComposerKeyDown(event, opts({ submit }));
    expect(submit).not.toHaveBeenCalled();
    expect(state.prevented).toBe(false);
  });

  it("a touch keyboard leaves Enter to the textarea too", () => {
    const submit = vi.fn();
    const { event, state } = keyDown();
    handleComposerKeyDown(event, opts({ submit, coarsePointer: true }));
    expect(submit).not.toHaveBeenCalled();
    expect(state.prevented).toBe(false);
  });

  it("an input method keeps its Enter", () => {
    const submit = vi.fn();
    const { event, state } = keyDown({ nativeEvent: { isComposing: true } });
    handleComposerKeyDown(event, opts({ submit }));
    expect(submit).not.toHaveBeenCalled();
    expect(state.prevented).toBe(false);
  });
});

describe("Input mobile and autofill contract", () => {
  it("keeps coarse-pointer 16px classes even when a caller passes text-xs", () => {
    render(<Input aria-label="Search" className="h-7 text-xs" />);
    const el = screen.getByLabelText("Search");
    const tokens = el.className.split(" ");
    expect(tokens).toContain("text-xs");
    expect(tokens).toContain("pointer-coarse:text-base");
    expect(tokens).toContain("md:pointer-coarse:text-base");
  });

  it("uses md:pointer-fine:text-sm by default so tablet touch viewports (>=768px) stay at 16px", () => {
    render(<Input aria-label="Plain" />);
    const el = screen.getByLabelText("Plain");
    const tokens = el.className.split(" ");
    expect(tokens).toContain("md:pointer-fine:text-sm");
    expect(tokens).not.toContain("md:text-sm");
    expect(tokens).toContain("pointer-coarse:text-base");
  });

  it("suppresses password/payment/address autofill on non-credential inputs by default", () => {
    render(<Input aria-label="Filter" />);
    const el = screen.getByLabelText("Filter");
    expect(el.getAttribute("autocomplete")).toBe("off");
    expect(el.getAttribute("data-form-type")).toBe("other");
    expect(el.getAttribute("data-1p-ignore")).toBe("");
    expect(el.getAttribute("data-lpignore")).toBe("true");
  });

  it("preserves password-manager autofill when autoComplete names a credential token", () => {
    render(<Input aria-label="Username" autoComplete="username" />);
    const user = screen.getByLabelText("Username");
    expect(user.getAttribute("autocomplete")).toBe("username");
    expect(user.getAttribute("data-form-type")).toBeNull();
    expect(user.getAttribute("data-1p-ignore")).toBeNull();
    expect(user.getAttribute("data-lpignore")).toBeNull();

    render(<Input aria-label="Password" type="password" autoComplete="current-password" />);
    const pass = screen.getByLabelText("Password");
    expect(pass.getAttribute("autocomplete")).toBe("current-password");
    expect(pass.getAttribute("data-form-type")).toBeNull();
    expect(pass.getAttribute("data-1p-ignore")).toBeNull();
    expect(pass.getAttribute("data-lpignore")).toBeNull();
  });
});

describe("Textarea mobile and autofill contract", () => {
  it("keeps coarse-pointer 16px classes and non-credential autofill suppression even when className overrides text size", () => {
    render(<Textarea aria-label="Prompt" className="text-xs" />);
    const el = screen.getByLabelText("Prompt");
    const tokens = el.className.split(" ");
    expect(tokens).toContain("text-xs");
    expect(tokens).toContain("pointer-coarse:text-base");
    expect(tokens).toContain("md:pointer-coarse:text-base");
    expect(el.getAttribute("autocomplete")).toBe("off");
    expect(el.getAttribute("data-form-type")).toBe("other");
    expect(el.getAttribute("data-1p-ignore")).toBe("");
    expect(el.getAttribute("data-lpignore")).toBe("true");
  });
});
