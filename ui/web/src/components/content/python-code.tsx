// meiva:note
// Python code block with syntax highlighting, a copy button and a streaming cursor on the last line. React.memo skips re-highlighting while `code` / `streaming` are unchanged, which matters during streaming: Prism tokenization is this component's main CPU cost. The ~85KB highlighter must stay out of the first-load bundle, so one shared lazy chunk carries it — every code block reads that chunk from context, and the prefetch triggers (a card header's pointer-enter/focus intent, CodeHighlighterPreloader's idle callback, the schedules row's expand button) warm the same chunk ahead of the click that needs it. Until it resolves the block renders plain source with the copy control; a rejected load drops the attempt so the next mount retries.
// meiva:link ui/web/src/docs/frontend-components/code-blocks.meiva.okf.md
"use client";

import { createContext, memo, useContext, useEffect, useState, type ReactNode } from "react";
import type * as PrismReactRenderer from "prism-react-renderer";
import type { Highlight as HighlightComponentType, PrismTheme } from "prism-react-renderer";

import { CopyButton } from "@/components/content/copy-button";
import { createLazyChunk, type LazyChunk } from "@/components/content/lazy-chunk";

const REAL_IMPORT = () => import("prism-react-renderer");

// meiva:note
// TEMPORARY (task #5732, blocked on #7548 merging): the import is a `let`, and the chunk behind the default provider is swappable, only so the two __…ForTests delegates at the bottom of this file keep serving the three test files whose rewrite waits on that PR. Production never swaps it; once that lands this collapses into one `const defaultChunk = createLazyChunk({ name, importModule: REAL_IMPORT })`.
// meiva:link ui/web/src/docs/frontend-components/test-only-imports.meiva.okf.md
let importHighlighterModule: () => Promise<typeof PrismReactRenderer> = REAL_IMPORT;

function createHighlighterChunk(): LazyChunk<typeof HighlightComponentType> {
  return createLazyChunk({
    name: "python-code",
    importModule: () => importHighlighterModule().then((module) => module.Highlight),
  });
}

let defaultChunk = createHighlighterChunk();

const activeChunk: LazyChunk<typeof HighlightComponentType> = {
  peek: () => defaultChunk.peek(),
  load: () => defaultChunk.load(),
  preload: () => defaultChunk.preload(),
};

const HighlighterChunkContext = createContext<LazyChunk<typeof HighlightComponentType>>(activeChunk);

export function PythonCodeHighlighterProvider({
  value,
  children,
}: {
  value: LazyChunk<typeof HighlightComponentType>;
  children: ReactNode;
}) {
  return <HighlighterChunkContext.Provider value={value}>{children}</HighlighterChunkContext.Provider>;
}

export function usePythonCodeHighlighter(): LazyChunk<typeof HighlightComponentType> {
  return useContext(HighlighterChunkContext);
}

const theme: PrismTheme = {
  plain: {
    color: "var(--foreground)",
    backgroundColor: "transparent",
  },
  styles: [
    { types: ["keyword"], style: { color: "var(--syntax-keyword)" } },
    { types: ["string", "char"], style: { color: "var(--syntax-string)" } },
    {
      types: ["comment"],
      style: { color: "var(--syntax-comment)", fontStyle: "italic" },
    },
    { types: ["number", "boolean"], style: { color: "var(--syntax-number)" } },
    { types: ["function", "decorator"], style: { color: "var(--syntax-function)" } },
    {
      types: ["builtin", "class-name"],
      style: { color: "var(--syntax-builtin)" },
    },
    { types: ["punctuation", "operator"], style: { color: "var(--syntax-punct)" } },
  ],
};

interface Props {
  code: string;
  streaming?: boolean;
}

export const PythonCode = memo(function PythonCode({ code, streaming = false }: Props) {
  const highlighter = usePythonCodeHighlighter();
  const [Highlight, setHighlight] = useState<typeof HighlightComponentType | null>(() =>
    highlighter.peek(),
  );

  useEffect(() => {
    if (Highlight !== null) return;
    let active = true;
    void highlighter.load().then(
      (Comp) => {
        if (active) setHighlight(() => Comp);
      },
      () => undefined,
    );
    return () => {
      active = false;
    };
  }, [Highlight, highlighter]);

  if (Highlight === null) {
    return <PlainCode code={code} streaming={streaming} />;
  }

  return (
    <Highlight theme={theme} code={code} language="python">
      {({ tokens, getLineProps, getTokenProps }) => (
        <div className="group relative">
          <CopyButton text={code} label="code" streaming={streaming} />
          <pre
            data-testid="python-code"
            data-streaming={streaming ? "1" : "0"}
            className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[13px] leading-relaxed my-2"
          >
            {tokens.map((line, i) => {
              const isLast = i === tokens.length - 1;
              const lineProps = getLineProps({ line });
              return (
                <div key={i} {...lineProps}>
                  {line.map((token, j) => (
                    <span key={j} {...getTokenProps({ token })} />
                  ))}
                  {streaming && isLast ? <BlinkCursor /> : null}
                </div>
              );
            })}
          </pre>
        </div>
      )}
    </Highlight>
  );
});

function PlainCode({ code, streaming }: Props) {
  return (
    <div className="group relative">
      <CopyButton text={code} label="code" streaming={streaming} />
      <pre
        data-testid="python-code"
        data-streaming={streaming ? "1" : "0"}
        className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[13px] leading-relaxed my-2"
      >
        {code}
        {streaming ? <BlinkCursor /> : null}
      </pre>
    </div>
  );
}

function BlinkCursor() {
  return (
    <span className="inline-block w-1.5 h-3 ml-0.5 bg-foreground/70 animate-pulse align-middle" />
  );
}

// meiva:note
// TEMPORARY (task #5732, blocked on #7548 merging): the pre-injection test seams as thin delegates onto the chunk above. Exactly three test files still call them (timeline/pinned-titles-autofollow.test.tsx, timeline/view/timeline-test-mock.tsx, app/control/schedules/page.test.tsx); #5732 deletes both, with the `let` and `activeChunk` indirection, once that PR has landed.
// meiva:link ui/web/src/docs/frontend-components/test-only-imports.meiva.okf.md
export function __resetHighlighterCacheForTests(): void {
  defaultChunk = createHighlighterChunk();
}

export function __setHighlighterImportForTests(
  fn: (() => Promise<typeof PrismReactRenderer>) | null,
): void {
  importHighlighterModule = fn ?? REAL_IMPORT;
}
