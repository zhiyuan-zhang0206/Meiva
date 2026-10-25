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

const defaultImport = async (): Promise<MarkdownMath> => {
  const [remarkMath, rehypeKatex] = await Promise.all([
    import("remark-math"),
    import("rehype-katex"),
    import("katex/dist/katex.min.css"),
  ]);
  return {
    remark: [remarkMath.default, remarkDisplayMathRecovery, remarkMathProseGuard],
    rehype: [[rehypeKatex.default, { throwOnError: false }]],
  };
};

const mathState: {
  importMath: () => Promise<MarkdownMath>;
  mathPromise: Promise<MarkdownMath> | null;
} = {
  importMath: defaultImport,
  mathPromise: null,
};

export function loadMarkdownMath(): Promise<MarkdownMath> {
  if (mathState.mathPromise !== null) return mathState.mathPromise;
  const promise = mathState.importMath().catch((error: unknown) => {
    if (mathState.mathPromise === promise) mathState.mathPromise = null;
    throw error;
  });
  mathState.mathPromise = promise;
  return promise;
}

export function __resetMarkdownMathForTests(): void {
  mathState.mathPromise = null;
}

export function __setMarkdownMathImportForTests(loader: (() => Promise<MarkdownMath>) | null): void {
  mathState.importMath = loader ?? defaultImport;
}
