// meiva:note
// AutoExpandTextarea applies the shared send rule: Enter sends, Shift+Enter breaks
// the line at the caret, an input method keeps its keys, and a touch keyboard keeps
// Enter as a line break. Each case here fails when that part of the rule is removed.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md

import { fireEvent, screen } from "@testing-library/react";
import { renderWithBrowser } from "@/test-support/render";
import { useState } from "react";
import { describe, expect, it, vi } from "vitest";

import { pressEnter } from "@/test-support/enter-key";

import { AutoExpandTextarea } from "./auto-expand-textarea";

function Field({
  coarsePointer,
  onSend,
}: {
  coarsePointer?: boolean;
  onSend: () => void;
}) {
  const [value, setValue] = useState("");
  return (
    <AutoExpandTextarea
      aria-label="Reply"
      value={value}
      onChange={(e) => setValue(e.target.value)}
      onSend={onSend}
      coarsePointer={coarsePointer}
    />
  );
}

function renderField(props: { coarsePointer?: boolean } = {}) {
  const onSend = vi.fn();
  renderWithBrowser(<Field {...props} onSend={onSend} />);
  return { onSend, box: screen.getByRole<HTMLTextAreaElement>("textbox", { name: "Reply" }) };
}

describe("AutoExpandTextarea", () => {
  it("Enter sends", () => {
    const { onSend, box } = renderField();
    fireEvent.change(box, { target: { value: "go" } });
    fireEvent.keyDown(box, { key: "Enter" });
    expect(onSend).toHaveBeenCalledTimes(1);
  });

  it("Shift+Enter inserts a newline at the caret and does not send", () => {
    const { onSend, box } = renderField();
    fireEvent.change(box, { target: { value: "firstsecond" } });
    box.setSelectionRange(5, 5);
    pressEnter(box, { shiftKey: true });
    expect(box.value).toBe("first\nsecond");
    expect(box.selectionStart).toBe(6);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("Enter committing an IME candidate neither sends nor loses the key", () => {
    const { onSend, box } = renderField();
    fireEvent.change(box, { target: { value: "\u4f60\u597d" } });
    expect(fireEvent.keyDown(box, { key: "Enter", isComposing: true })).toBe(true);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("a touch keyboard inserts a newline instead of sending", () => {
    const { onSend, box } = renderField({ coarsePointer: true });
    fireEvent.change(box, { target: { value: "two lines" } });
    box.setSelectionRange(3, 3);
    pressEnter(box);
    expect(box.value).toBe("two\n lines");
    expect(box.selectionStart).toBe(4);
    expect(onSend).not.toHaveBeenCalled();
  });
});
