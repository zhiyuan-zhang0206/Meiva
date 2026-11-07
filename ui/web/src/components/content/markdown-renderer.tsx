// meiva:note
// Raw HTML renders only through the shared sanitize allowlist — scripts, styles, frames and event handlers never reach the DOM; urlTransform keeps HTTP(S)/mailto/page-anchor URLs and sends the rest to about:blank; external HTTP(S) links open in a new tab with rel=noopener noreferrer, and explicit document resolvers may replace registered local references with authenticated resource URLs. remark-cjk-friendly, plus the remark-cjk-friendly-gfm-strikethrough companion required while remark-gfm is in the pipeline, restores bold and strikethrough when delimiters sit against CJK letters or punctuation-led content (curly quotes, straight quotes, corner brackets — task #3314); plain CJK and ASCII emphasis stays unchanged. Both GFM tilde tokenizers disable single-tilde strike-through so home paths and approximations like the reported ~61G and ~/work stay literal, only the double form strikes, and tildes inside inline code are untouched (task #3653). remark-breaks turns single soft line breaks into <br> tags so model replies and natural prose preserve author line breaks rather than collapsing them to spaces. Fenced code retains whitespace even without a language tag, renders as one pre block, and exposes copy controls; a fenced python block routes to the PythonCode surface while inline code never does. Memoization avoids re-parsing unchanged sibling content during streaming. The suite mocks PythonCode, so the tests never run Prism.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md
"use client";

import { createContext, memo, useCallback, useContext, useEffect, useMemo, useState, type ReactNode } from "react";
import ReactMarkdown, { type Components, type UrlTransform } from "react-markdown";
import rehypeRaw from "rehype-raw";
import rehypeSanitize, { defaultSchema } from "rehype-sanitize";
import rehypeSlug from "rehype-slug";
import remarkBreaks from "remark-breaks";
import remarkCjkFriendly from "remark-cjk-friendly";
import remarkCjkFriendlyGfmStrikethrough from "remark-cjk-friendly-gfm-strikethrough";
import remarkGfm from "remark-gfm";
import type { Plugin, PluggableList } from "unified";

import { CopyButton } from "@/components/content/copy-button";
import { isFilePath } from "@/components/content/file-path-utils";
import { PythonCode } from "@/components/content/python-code";
import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import { MIN_W_0 } from "@/lib/layout/layout";
import {
  defaultMarkdownMath,
  hasMathSyntax,
  type MarkdownMath,
  type MarkdownMathSource,
} from "@/lib/markdown/math";
import remarkAutolinkDelimiter from "@/lib/markdown/remark-autolink-delimiter";
import remarkCjkLinkBoundary from "@/lib/markdown/remark-cjk-link-boundary";
import { useStore } from "@/lib/state/store";
import { cn } from "@/lib/format/utils";

// meiva:note
// The math chunk arrives as a value: production wires nothing and every surface reads the module's
// own source, while a test wraps its tree in `MarkdownMathProvider` with a source built from a fake
// importer — the same shape the code chunk uses, and the reason no shipped module exports a test-only
// setter for it.
// meiva:link ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md
const MarkdownMathContext = createContext<MarkdownMathSource>(defaultMarkdownMath);

export function MarkdownMathProvider({
  value,
  children,
}: {
  value: MarkdownMathSource;
  children: ReactNode;
}) {
  return <MarkdownMathContext.Provider value={value}>{children}</MarkdownMathContext.Provider>;
}

export function useMarkdownMath(): MarkdownMathSource {
  return useContext(MarkdownMathContext);
}

const DANGEROUS_PROTOCOLS = /^(javascript|vbscript):/i;

export const urlTransform: UrlTransform = (url: string): string => {
  const trimmed = url.trim();
  if (DANGEROUS_PROTOCOLS.test(trimmed)) {
    return "about:blank";
  }
  return trimmed;
};

// meiva:link ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md
const sanitizeSchema = {
  ...defaultSchema,
  clobberPrefix: "",
  strip: [...(defaultSchema.strip ?? []), "style", "iframe", "template"],
  tagNames: [...(defaultSchema.tagNames ?? []), "figure", "figcaption", "mark"],
  protocols: {
    ...defaultSchema.protocols,
    href: [...(defaultSchema.protocols?.href ?? ["http", "https", "mailto"]), "file"],
    src: [...(defaultSchema.protocols?.src ?? ["http", "https"]), "file"],
  },
  attributes: {
    ...defaultSchema.attributes,
    // meiva:note
    // `language-math` is what lets rehype-katex replace the wrapping <pre> with
    // its display block; without it a display formula keeps code-block chrome.
    // meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md
    code: [...(defaultSchema.attributes?.code ?? []), ["className", "language-math", "math-inline", "math-display"]],
  },
};

const baseRemarkPlugins: PluggableList = [
  remarkBreaks,
  [remarkGfm, { singleTilde: false }],
  remarkCjkFriendly,
  [remarkCjkFriendlyGfmStrikethrough, { singleTilde: false }],
  remarkCjkLinkBoundary,
  remarkAutolinkDelimiter,
];

interface HastLike {
  type: string;
  tagName?: string;
  properties?: Record<string, unknown>;
  children?: HastLike[];
}

type UrlResolver = (url: string) => string | null | undefined;

const rehypeResolveUrls: Plugin<[UrlResolver]> = (resolve) => (tree) => {
  const walk = (node: HastLike): void => {
    for (const child of node.children ?? []) walk(child);
    if (node.type !== "element" || node.properties === undefined) return;
    const href = node.properties.href;
    const src = node.properties.src;
    if (node.tagName === "a" && typeof href === "string" && !href.startsWith("#")) {
      node.properties.href = resolve(href);
    }
    if (node.tagName === "img" && typeof src === "string") {
      node.properties.src = resolve(src);
    }
  };
  walk(tree);
};

// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md
const inlineElements = ["a", "br", "code", "del", "em", "strong", "span", "sub", "sup", "p"];

const baseRehypePlugins = (resolve: UrlTransform, inline: boolean): PluggableList => [
  rehypeRaw,
  [rehypeResolveUrls, resolve],
  [rehypeSanitize, inline ? { ...sanitizeSchema, tagNames: inlineElements } : sanitizeSchema],
  rehypeSlug,
];

const createDefaultComponents = (onOpenFile: (path: string, lineRange?: string) => void): Components => ({
  pre: ({ node: _node, children }) => <>{children}</>,
  a: ({ node: _node, href, children, ...props }) => {
    const external = /^https?:/i.test(href ?? "");
    const fileMatch = href ? isFilePath(href) : null;
    const isFile = Boolean(fileMatch?.isPath);
    if (isFile && fileMatch) {
      return (
        <a
          href={href}
          {...props}
          data-testid="file-path-link"
          onClick={(e) => {
            if (e.metaKey || e.ctrlKey || e.shiftKey || e.altKey || e.button !== 0) return;
            e.preventDefault();
            onOpenFile(fileMatch.path, fileMatch.lineRange);
          }}
          className={cn("cursor-pointer", props.className)}
        >
          {children}
        </a>
      );
    }
    return external ? (
      <a href={href} {...props} target="_blank" rel="noopener noreferrer">
        {children}
      </a>
    ) : (
      <a href={href} {...props}>
        {children}
      </a>
    );
  },
  code: ({ node: _node, className, children, ...props }) => {
    const lang = /language-(\w+)/.exec(className ?? "")?.[1];

    const text = typeof children === "string" ? children : "";
    if (lang === undefined && !text.includes("\n")) {
      const fileMatch = isFilePath(text);
      if (fileMatch.isPath) {
        return (
          <span
            role="link"
            tabIndex={0}
            data-testid="file-path-code"
            onClick={(e) => {
              if (e.currentTarget.closest("a")) return;
              e.stopPropagation();
              onOpenFile(fileMatch.path, fileMatch.lineRange);
            }}
            onKeyDown={(e) => {
              if (e.currentTarget.closest("a")) return;
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                e.stopPropagation();
                onOpenFile(fileMatch.path, fileMatch.lineRange);
              }
            }}
            className="underline underline-offset-2 cursor-pointer"
          >
            <code className={className} {...props}>
              {children}
            </code>
          </span>
        );
      }
      return (
        <code className={className} {...props}>
          {children}
        </code>
      );
    }
    const codeStr = text.replace(/\n$/, "");
    if (lang === "python") {
      return <PythonCode code={codeStr} />;
    }
    return (
      <div className="group relative">
        <CopyButton text={codeStr} label={lang ? `${lang} code` : "code"} />
        <pre>
          <code className={className}>{codeStr}</code>
        </pre>
      </div>
    );
  },
});

const createInlineComponents = (onOpenFile: (path: string, lineRange?: string) => void): Components => ({
  p: ({ children }) => <>{children}</>,
  code: ({ children }) => {
    const text = typeof children === "string" ? children : "";
    const fileMatch = isFilePath(text);
    if (fileMatch.isPath) {
      return (
        <span
          role="link"
          tabIndex={0}
          data-testid="file-path-code"
          onClick={(e) => {
            e.stopPropagation();
            onOpenFile(fileMatch.path, fileMatch.lineRange);
          }}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              e.stopPropagation();
              onOpenFile(fileMatch.path, fileMatch.lineRange);
            }
          }}
          className="underline underline-offset-2 cursor-pointer"
        >
          <code>{children}</code>
        </span>
      );
    }
    return <code>{children}</code>;
  },
});

export interface ChatMarkdownProps {
  content: string;
  components?: Components;
  resolveUrl?: UrlTransform;
  inline?: boolean;
  onOpenFile?: (path: string, lineRange?: string) => void;
}

// meiva:note
// Headings gain stable, deduplicated slug ids and same-document # links keep targeting them; GFM footnote reference/definition anchor pairs still match after sanitizing; the To user panel treats page-anchor links as in-panel navigation rather than external opens.
// meiva:link ui/web/src/docs/frontend-components/markdown-safe-content.meiva.okf.md
export const ChatMarkdownRenderer = memo(function ChatMarkdownRenderer({
  content,
  components: overrides,
  resolveUrl,
  inline = false,
  onOpenFile,
}: ChatMarkdownProps) {
  const [math, setMath] = useState<MarkdownMath | null>(null);
  const mathSource = useMarkdownMath();
  const activeId = useStore((s) => s.activeId);

  const defaultOpenFile = useCallback(
    (path: string, lineRange?: string) => {
      if (activeId !== null) {
        useWorkspaceFileStore.getState().openFile({ agentId: activeId, path, lineRange });
      }
    },
    [activeId],
  );

  const effectiveOpenFile = onOpenFile ?? defaultOpenFile;

  const defaultComponents = useMemo(
    () => createDefaultComponents(effectiveOpenFile),
    [effectiveOpenFile],
  );
  const defaultInlineComponents = useMemo(
    () => createInlineComponents(effectiveOpenFile),
    [effectiveOpenFile],
  );

  const needsMath = hasMathSyntax(content);
  useEffect(() => {
    if (!needsMath || math !== null) return;
    let active = true;
    void mathSource.load().then(
      (loaded) => {
        if (active) setMath(loaded);
      },
      () => undefined,
    );
    return () => {
      active = false;
    };
  }, [needsMath, math, mathSource]);

  const activeComponents = useMemo(() => {
    if (inline) {
      return overrides
        ? { ...defaultComponents, ...defaultInlineComponents, ...overrides }
        : { ...defaultComponents, ...defaultInlineComponents };
    }
    return overrides ? { ...defaultComponents, ...overrides } : defaultComponents;
  }, [inline, overrides, defaultComponents, defaultInlineComponents]);

  const Wrapper = inline ? "span" : "div";
  return (
    <Wrapper className={cn("chat-md font-sans", MIN_W_0)}>
      <ReactMarkdown
        remarkPlugins={[...baseRemarkPlugins, ...(math?.remark ?? [])]}
        rehypePlugins={[...baseRehypePlugins(resolveUrl ?? urlTransform, inline), ...(math?.rehype ?? [])]}
        urlTransform={resolveUrl ?? urlTransform}
        components={activeComponents}
      >
        {content}
      </ReactMarkdown>
    </Wrapper>
  );
});
