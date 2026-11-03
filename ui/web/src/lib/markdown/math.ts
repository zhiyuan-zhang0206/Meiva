// meiva:note
// LaTeX is loaded on demand. `remark-math` runs with `singleDollarTextMath: false`
// so `$$...$$` and `mdast-util-math` remain upstream while `remarkLatexMath`
// enforces Pandoc and prose boundaries on `$...$` at tokenize time and adds
// `\(...\)` / `\[...\]`; `remarkDisplayMathRecovery` promotes single-paragraph
// `$$...$$` and `\[...\]` spans into display blocks.
// meiva:link ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md
import type { PluggableList } from "unified";

import remarkDisplayMathRecovery from "./remark-display-math-recovery";
import remarkLatexMath from "./remark-latex-math";

export { hasMathSyntax } from "./latex-math-guard";

export interface MarkdownMath {
  remark: PluggableList;
  rehype: PluggableList;
}

const defaultImport = (): Promise<MarkdownMath> =>
  Promise.all([
    import("remark-math"),
    import("rehype-katex"),
    import("katex/dist/katex.min.css"),
  ]).then(([remarkMath, rehypeKatex]) => ({
    remark: [
      [remarkMath.default, { singleDollarTextMath: false }],
      remarkLatexMath,
      remarkDisplayMathRecovery,
    ],
    rehype: [[rehypeKatex.default, { throwOnError: false }]],
  }));

// meiva:note
// The math chunk is a value, not module state: one deferred import per source, the first attempt
// shared by every caller, and a failed attempt dropped so the next caller retries. Production
// builds the module's own source; a test builds its own around a fake importer instead of reaching
// for a setter on a shipped module.
export interface MarkdownMathSource {
  load: () => Promise<MarkdownMath>;
}

export function createMarkdownMath(
  importMath: () => Promise<MarkdownMath> = defaultImport,
): MarkdownMathSource {
  let pending: Promise<MarkdownMath> | null = null;
  return {
    load: () => {
      if (pending !== null) return pending;
      const attempt = importMath().catch((error: unknown) => {
        if (pending === attempt) pending = null;
        throw error;
      });
      pending = attempt;
      return attempt;
    },
  };
}

export const defaultMarkdownMath: MarkdownMathSource = createMarkdownMath();
