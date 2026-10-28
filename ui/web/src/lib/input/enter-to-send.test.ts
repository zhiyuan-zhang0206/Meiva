import { describe, expect, it } from "vitest";

import { isImeComposition, isSendEnter, type KeyStroke } from "./enter-to-send";

// meiva:note
// A plain Enter on a mouse or trackpad, as a React keydown hands it over; each case
// overrides the one field it is about.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md
function stroke(overrides: Partial<KeyStroke> = {}): KeyStroke {
  return {
    key: "Enter",
    shiftKey: false,
    keyCode: 13,
    nativeEvent: { isComposing: false },
    ...overrides,
  };
}

describe("isSendEnter", () => {
  it("a plain Enter sends", () => {
    expect(isSendEnter(stroke(), false)).toBe(true);
  });

  it("Shift+Enter breaks the line instead of sending", () => {
    expect(isSendEnter(stroke({ shiftKey: true }), false)).toBe(false);
  });

  it("Enter that commits an IME candidate does not send (CJK input)", () => {
    expect(isSendEnter(stroke({ nativeEvent: { isComposing: true } }), false)).toBe(false);
  });

  it("the legacy Process key is an IME key even when isComposing is false", () => {
    expect(isSendEnter(stroke({ key: "Process" }), false)).toBe(false);
  });

  it("the legacy key code 229 marks an IME keydown", () => {
    expect(isSendEnter(stroke({ keyCode: 229 }), false)).toBe(false);
  });

  it("a touch keyboard keeps Enter as a line break (no Shift key to reach it)", () => {
    expect(isSendEnter(stroke(), true)).toBe(false);
  });

  it("every other key is left alone", () => {
    for (const key of ["a", "ArrowRight", "Tab", "Escape", "Backspace"]) {
      expect(isSendEnter(stroke({ key }), false)).toBe(false);
    }
  });
});

describe("isImeComposition", () => {
  it("reads the composition flag, the Process key and the legacy key code", () => {
    expect(isImeComposition(stroke())).toBe(false);
    expect(isImeComposition(stroke({ nativeEvent: { isComposing: true } }))).toBe(true);
    expect(isImeComposition(stroke({ key: "Process" }))).toBe(true);
    expect(isImeComposition(stroke({ keyCode: 229 }))).toBe(true);
  });

  it("tolerates a stroke with no native event", () => {
    expect(isImeComposition({ key: "Enter" })).toBe(false);
  });
});
