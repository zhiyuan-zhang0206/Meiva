// meiva:note
// LaTeX is loaded on demand, and the recovery plugin runs before the prose
// guard: a promoted `$$` span is a block node the guard never inspects.
// meiva:link ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md
import type { PluggableList } from "unified";

import remarkDisplayMathRecovery from "./remark-display-math-recovery";
import remarkMathProseGuard from "./remark-math-prose-guard";

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
    remark: [remarkMath.default, remarkDisplayMathRecovery, remarkMathProseGuard],
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
