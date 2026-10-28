// meiva:note
// The composer's keydown handler on its own: the keys it consumes and the keys it
// leaves to the textarea, with the pointer handed in rather than read.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md

import { describe, expect, it, vi } from "vitest";

import {
  handleComposerKeyDown,
  type ComposerKeyDown,
} from "./composer-helpers";

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
