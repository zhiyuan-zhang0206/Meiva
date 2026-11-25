// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md

import { describe, expect, it } from "vitest";

import { repairStreamingMarkdown as repair } from "./streaming-repair";

describe("repairStreamingMarkdown", () => {
  it.each([
    ["plain text is untouched", "hello world", "hello world"],
    ["empty", "", ""],
    ["closed bold is untouched", "a **b** c", "a **b** c"],
    ["open bold is closed", "a **bo", "a **bo**"],
    ["open italic is closed", "a *it", "a *it*"],
    ["open underscore italic at a word boundary", "a _it", "a _it_"],
    ["snake_case is not emphasis", "call snake_case_name now", "call snake_case_name now"],
    ["open strike is closed", "a ~~gone", "a ~~gone~~"],
    ["nested open markers close in reverse order", "**a *b", "**a *b***"],
    ["a dangling opener with nothing after it is hidden", "a **", "a "],
    ["a dangling single star is hidden", "a *", "a "],
    ["a bullet star is not emphasis", "* item", "* item"],
    ["multiplication stars are not emphasis", "2 * 3 * 4", "2 * 3 * 4"],
    ["open inline code is closed", "run `ls -", "run `ls -`"],
    ["empty open inline code is hidden", "run `", "run "],
    ["closed inline code hides emphasis characters inside", "`a*b` and **c", "`a*b` and **c**"],
    ["escaped star is literal", "a \\*b", "a \\*b"],
    ["partial link destination keeps only the label", "see [docs](https://exa", "see docs"],
    ["complete link is untouched", "see [docs](https://example.com)", "see [docs](https://example.com)"],
    ["partial image is dropped entirely", "x ![alt](https://exa", "x "],
    ["underscores inside a half-typed URL are not emphasis", "[a](https://x.com/a_b_c", "a"],
    ["a closed paragraph is not repaired by a later open one", "**done**\n\nnext *it", "**done**\n\nnext *it*"],
    ["emphasis does not leak across a blank line", "open *a\n\nnext", "open *a\n\nnext"],
    ["half-typed fence opener is hidden", "text\n\n``", "text\n\n"],
    ["unclosed fence is left to the renderer", "```py\nx = **2", "```py\nx = **2"],
    ["closed fence then open bold", "```\ncode\n```\nthen **b", "```\ncode\n```\nthen **b**"],
    ["CJK emphasis", "\u8fd9\u662f**\u52a0\u7c97\u7684\u6587", "\u8fd9\u662f**\u52a0\u7c97\u7684\u6587**"],
  ])("%s", (_name, input, expected) => {
    expect(repair(input)).toBe(expected);
  });

  it("is idempotent on its own output", () => {
    for (const s of ["a **bo", "run `ls", "**a *b", "x [l](http://u"]) {
      const once = repair(s);
      expect(repair(once)).toBe(once);
    }
  });

  it("never throws on every prefix of a mixed document", () => {
    const doc = "## T\n\n1. **a** and `b` [c](http://d_e)\n\n```py\nx=1\n```\n\n~~s~~ *i* __b__ _u_ \u4e2d\u6587**\u7c97**\n";
    for (let n = 0; n <= doc.length; n++) expect(() => repair(doc.slice(0, n))).not.toThrow();
  });
});
