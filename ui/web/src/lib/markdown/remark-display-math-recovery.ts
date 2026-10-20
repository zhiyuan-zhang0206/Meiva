import type { Nodes, Paragraph, Parents, Root } from "mdast";
import type { VFile } from "vfile";

import { isParent } from "@/lib/markdown/remark-shared";

// meiva:note
// remark-math makes a display block only when `$$` starts a block: written on its
// own line but glued to the prose line above (no blank line between), the pair
// parses as *inline* math, so a `\frac` + `\left(...\right)` pair lands mid
// paragraph, wraps at the first narrow column and stretches the line height - the
// 2026-10-20 report where a phone showed a two-formula paragraph as rubble. The
// delimiters are unambiguous in the source, so this reads the span back from the
// file and promotes it to a display math node, splitting the paragraph around it.
// It is a source-level recovery, not a reading of intent: `$...$` stays inline,
// and a `$$` pair the author meant literally is promoted all the same.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md

type MathNode = Extract<Nodes, { type: "math" }>;
type Block = Paragraph | MathNode;

type InlineMathNode = Extract<Nodes, { type: "inlineMath" }>;

function isDisplayDelimited(node: Nodes, source: string): node is InlineMathNode {
  if (node.type !== "inlineMath") return false;
  const start = node.position?.start.offset;
  const end = node.position?.end.offset;
  if (start === undefined || end === undefined) return false;
  const raw = source.slice(start, end);
  return raw.length >= 4 && raw.startsWith("$$") && raw.endsWith("$$");
}


// meiva:note
// The node carries the same `data` shape `mdast-util-math` gives its own flow
// nodes (`pre` around a `code.language-math.math-display`): mdast-util-to-hast
// has no handler for `math`, and `rehype-katex` replaces the `pre` only when the
// `code` keeps `language-math`. A bare `math` node renders as its raw TeX text.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md
function displayMath(value: string): MathNode {
  return {
    type: "math",
    value,
    data: {
      hName: "pre",
      hChildren: [
        {
          type: "element",
          tagName: "code",
          properties: { className: ["language-math", "math-display"] },
          children: [{ type: "text", value }],
        },
      ],
    },
  };
}

function promote(paragraph: Paragraph, source: string): Block[] | null {
  if (!paragraph.children.some((child) => isDisplayDelimited(child, source))) return null;
  const blocks: Block[] = [];
  let run: Paragraph["children"] = [];
  const flush = (): void => {
    if (run.length === 0) return;
    blocks.push({ type: "paragraph", children: run });
    run = [];
  };
  for (const child of paragraph.children) {
    if (isDisplayDelimited(child, source)) {
      flush();
      blocks.push(displayMath(child.value));
      continue;
    }
    run.push(child);
  }
  flush();
  return blocks;
}

function walk(parent: Parents, source: string): void {
  for (let index = 0; index < parent.children.length; index += 1) {
    const child: Nodes = parent.children[index];
    if (child.type === "paragraph") {
      const blocks = promote(child, source);
      if (blocks !== null) {
        parent.children.splice(index, 1, ...blocks);
        index += blocks.length - 1;
        continue;
      }
    }
    if (isParent(child)) walk(child, source);
  }
}

export default function remarkDisplayMathRecovery() {
  return (tree: Root, file: VFile): void => {
    walk(tree, String(file.value));
  };
}
