// meiva:note
// One rule for every send-type text field: the conversation composer, a notice
// reply, a notice question's answer and comment, and the fork/resurrect prompt.
// Enter sends. Shift+Enter breaks the line. A keystroke an input method is
// consuming is left to that input method — committing a CJK candidate with Enter
// must neither send nor be swallowed — and a touch keyboard keeps Enter as a line
// break, because the on-screen keyboard has no Shift key to reach the other side;
// the visible Send button submits there. The primary pointer arrives as an
// argument (each field reads it with use-coarse-pointer.ts), so this module stays
// a pure decision.
// meiva:link ui/web/src/docs/frontend-components/enter-to-send.meiva.okf.md

export interface KeyStroke {
  key: string;
  shiftKey?: boolean;
  keyCode?: number;
  nativeEvent?: { isComposing?: boolean };
}

export function isImeComposition(stroke: KeyStroke): boolean {
  return (
    stroke.nativeEvent?.isComposing === true ||
    stroke.key === "Process" ||
    stroke.keyCode === 229
  );
}

export function isSendEnter(stroke: KeyStroke, coarsePointer: boolean): boolean {
  return (
    stroke.key === "Enter" &&
    !stroke.shiftKey &&
    !isImeComposition(stroke) &&
    !coarsePointer
  );
}
