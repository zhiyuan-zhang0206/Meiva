import type { Nodes, Root } from "mdast";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkGfm from "remark-gfm";
import remarkMath from "remark-math";
import remarkParse from "remark-parse";
import { unified } from "unified";
import { describe, expect, it } from "vitest";

import { hasMathSyntax, isValidSingleDollarMath } from "./latex-math-guard";
import remarkDisplayMathRecovery from "./remark-display-math-recovery";
import remarkLatexMath from "./remark-latex-math";

function parseTree(markdown: string): Root {
  const processor = unified()
    .use(remarkParse)
    .use(remarkGfm, { singleTilde: false })
    .use(remarkCjkFriendly)
    .use(remarkMath, { singleDollarTextMath: false })
    .use(remarkLatexMath)
    .use(remarkDisplayMathRecovery);
  return processor.runSync(processor.parse(markdown), markdown) as Root;
}

function collectByType<T extends Nodes["type"]>(
  node: Root | Nodes,
  type: T,
): Extract<Nodes, { type: T }>[] {
  const out: Extract<Nodes, { type: T }>[] = [];
  const visit = (current: Root | Nodes): void => {
    if (current.type === type) {
      out.push(current as Extract<Nodes, { type: T }>);
    }
    if ("children" in current) {
      for (const child of current.children) visit(child);
    }
  };
  visit(node);
  return out;
}

describe("latex-math-guard", () => {
  it("detects dollar, LaTeX paren/bracket, and fenced math triggers", () => {
    expect(hasMathSyntax("plain prose [1] (2)")).toBe(false);
    expect(hasMathSyntax("inline $x$")).toBe(true);
    expect(hasMathSyntax("inline \\(x\\)")).toBe(true);
    expect(hasMathSyntax("display \\[x = 1\\]")).toBe(true);
    expect(hasMathSyntax("```math\nx^2\n```")).toBe(true);
  });

  it("allows nested braces inside \\text groups while rejecting outside CJK", () => {
    expect(isValidSingleDollarMath("\\text{\u6ee1\u8db3{\u6839}\u6761\u4ef6} + x^2")).toBe(true);
    expect(isValidSingleDollarMath("30 \u4e0a\u4e0b**\uff0c")).toBe(false);
    expect(isValidSingleDollarMath("A^{**}")).toBe(true);
    expect(isValidSingleDollarMath("30** and **")).toBe(false);
  });
});

describe("remarkLatexMath", () => {
  it("keeps currency ratios inside bold literal instead of pairing $ signs", () => {
    const tree = parseTree("\u5f53\u524d\u7528\u91cf **$0.51 / $40.00**\u3002");
    expect(collectByType(tree, "inlineMath")).toHaveLength(0);
    const strongs = collectByType(tree, "strong");
    expect(strongs).toHaveLength(1);
    expect(strongs[0].children).toMatchObject([{ type: "text", value: "$0.51 / $40.00" }]);
  });

  it("preserves bold delimiters across CJK currency pairs on the same line", () => {
    const cases = [
      {
        input: "11 \u6708\u5927\u6982\u7387\u843d\u5728 **$30 \u4e0a\u4e0b**\uff0c$40 \u8fd9\u6761\u63d0\u9192\u7ebf\u3002",
        expectedBold: "$30 \u4e0a\u4e0b",
      },
      {
        input: "10 \u6708\uff1aActions \u6bdb\u989d $23.11\uff0c\u5176\u4e2d **$22.61 \u662f\u5b58\u50a8**\uff1b\u5206\u949f\u6570\u6bdb\u989d $18.48\u3001\u5b9e\u6263 $0.48\u3002",
        expectedBold: "$22.61 \u662f\u5b58\u50a8",
      },
      {
        input: "11 \u6708\u5934\u4e09\u5929\uff1a\u6bdb\u989d $7.85\u3001\u5b9e\u6263 **$0.51**\uff0c\u540c\u6837\u5168\u662f\u5b58\u50a8\u3002",
        expectedBold: "$0.51",
      },
      {
        input: "\u73b0\u5728\u8ba1\u8d39\u5b58\u50a8\uff1a**\u5236\u54c1 13.54 GB\uff08\u2248$2.9/\u6708\uff09**\uff0c\u539f 51.7 GB\uff08\u2248$12.4/\u6708\uff09\u3002",
        expectedBold: "\u5236\u54c1 13.54 GB\uff08\u2248$2.9/\u6708\uff09",
      },
    ];

    for (const { input, expectedBold } of cases) {
      const tree = parseTree(input);
      expect(collectByType(tree, "inlineMath")).toHaveLength(0);
      const strongs = collectByType(tree, "strong");
      expect(strongs).toHaveLength(1);
      expect(strongs[0].children).toMatchObject([{ type: "text", value: expectedBold }]);
    }
  });

  it("handles currency and genuine single-dollar math in the same sentence", () => {
    const cjkMixed = parseTree("\u5355\u4ef7 **$30 \u4e0a\u4e0b**\uff0c\u590d\u6742\u5ea6\u662f $O(n^2)$\u3002");
    expect(collectByType(cjkMixed, "strong")[0]?.children).toMatchObject([
      { type: "text", value: "$30 \u4e0a\u4e0b" },
    ]);
    expect(collectByType(cjkMixed, "inlineMath")).toMatchObject([{ value: "O(n^2)" }]);

    const englishMixed = parseTree(
      "Compare **$30** and **$n$**, ($30) vs ($k + 1$), or $0.12, ~$14/mo ($O(1)$ overhead) at US$10/$20 and $1/dt$.",
    );
    const strongs = collectByType(englishMixed, "strong");
    expect(strongs).toHaveLength(2);
    expect(strongs[0].children).toMatchObject([{ type: "text", value: "$30" }]);
    expect(strongs[1].children).toMatchObject([{ type: "inlineMath", value: "n" }]);
    expect(collectByType(englishMixed, "inlineMath").map((node) => node.value)).toEqual([
      "n",
      "k + 1",
      "O(1)",
      "1/dt",
    ]);
  });

  it("supports escaped literal \\$ inside and adjacent to single-dollar math", () => {
    const tree = parseTree("Escaped \\$$x$ and $y$\\$ plus $\\text{cost } \\$5$.");
    expect(collectByType(tree, "inlineMath").map((node) => node.value)).toEqual([
      "x",
      "y",
      "\\text{cost } \\$5",
    ]);
  });

  it("parses \\(...\\) inline math and \\[...\\] display math while keeping \\[1\\] literal", () => {
    const inline = parseTree("Complexity is \\( O(n \\log n) \\) for \\(n \\ge 1\\).");
    expect(collectByType(inline, "inlineMath").map((node) => node.value.trim())).toEqual([
      "O(n \\log n)",
      "n \\ge 1",
    ]);

    const fenced = parseTree("Prose above:\n\\[\nx = 1\n- y\n\\]\nSee \\[1\\] and \\[non-null\\].");
    expect(collectByType(fenced, "math")).toMatchObject([{ value: "x = 1\n- y" }]);
    expect(collectByType(fenced, "inlineMath")).toHaveLength(0);

    const listIndented = parseTree("1. Item:\n   \\[\n   E = mc^2\n   \\]");
    expect(collectByType(listIndented, "math")).toMatchObject([{ value: "E = mc^2" }]);

    const glued = parseTree("\u7ed3\u679c\uff1a\n\\[E = mc^2\\]");
    expect(collectByType(glued, "math")).toMatchObject([{ value: "E = mc^2" }]);
  });
});
