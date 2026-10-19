// meiva:link ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md
import type { Nodes, Parents, Root } from "mdast";

import { isParent } from "@/lib/markdown/remark-shared";

// meiva:note
// remark-math pairs the next two `$` on a line however far apart they sit, so a `$` written as a plain symbol ("the root condition $, and ...") turns the prose up to the next `$` into inline math, which KaTeX then paints as an error. A span carrying prose - CJK outside \text{...}, fullwidth punctuation, or an inline-code backtick - is demoted back to its literal $...$ text; ASCII-only spans and every display $$...$$ keep rendering as math. This is a heuristic, not a reading of intent: \text{<CJK>} stays math, display math is never touched, and ASCII-only prose between two stray `$` still parses as math and can still render an error - only knowing the author's intent (or escaping the stray `$` as \$) closes that shape.
// meiva:link ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md
const PROSE_IN_MATH =
  /\p{Script=Han}|\p{Script=Hiragana}|\p{Script=Katakana}|\p{Script=Hangul}|[\u3000-\u303F\uFF00-\uFFEF]/u;
const TEXT_GROUP = /\\text\{[^{}]*\}/g;

function carriesProse(value: string): boolean {
  return value.includes("`") || PROSE_IN_MATH.test(value.replace(TEXT_GROUP, ""));
}

function demoteProseMath(parent: Parents): void {
  for (let index = 0; index < parent.children.length; index += 1) {
    const child: Nodes = parent.children[index];
    if (child.type === "inlineMath" && carriesProse(child.value)) {
      parent.children[index] = { type: "text", value: `$${child.value}$` };
      continue;
    }
    if (isParent(child)) {
      demoteProseMath(child);
    }
  }
}

export default function remarkMathProseGuard() {
  return (tree: Root): void => {
    demoteProseMath(tree);
  };
}
