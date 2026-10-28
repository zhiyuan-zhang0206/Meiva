// meiva:note
// Keyboard helpers for tests that assert what a send-type text field holds after
// a key, not merely that nothing threw. pressEnter dispatches the keydown and,
// when the component did not cancel it, performs the edit a browser performs —
// through the textarea's own value setter, because React patches that property on
// the element and a native edit is what makes it see a change.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md

import { fireEvent } from "@testing-library/react";

function setTextareaValue(box: HTMLTextAreaElement, value: string): void {
  const descriptor = Object.getOwnPropertyDescriptor(
    HTMLTextAreaElement.prototype,
    "value",
  );
  const setter: unknown = descriptor ? Reflect.get(descriptor, "set") : undefined;
  if (typeof setter !== "function") {
    throw new Error("HTMLTextAreaElement has no value setter");
  }
  Reflect.apply(setter, box, [value]);
}

export function pressEnter(
  box: HTMLTextAreaElement,
  init: { shiftKey?: boolean; isComposing?: boolean; keyCode?: number } = {},
): void {
  if (!fireEvent.keyDown(box, { key: "Enter", ...init })) return;
  const start = box.selectionStart;
  setTextareaValue(box, `${box.value.slice(0, start)}\n${box.value.slice(box.selectionEnd)}`);
  box.setSelectionRange(start + 1, start + 1);
  fireEvent.input(box);
}
