// meiva:note
// ChatMarkdown is a lazy boundary: the renderer (the react-markdown chain) is a deferred chunk, so route bundles carry only the boundary and a cold first paint shows the same content as plain text inside the shared chat-md wrapper. A resolved renderer renders Markdown on the first paint; a pending one upgrades the mounted tree when it arrives, and a superseded or unmounted attempt never writes back. Prefetch starts when the boundary module evaluates; one chunk deduplicates that import for every surface, a rejected load drops the attempt so the next one retries, and the fallback stays in place until it succeeds.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md
"use client";

import { createContext, memo, useContext, useEffect, useState, type ReactNode } from "react";

import { createLazyChunk, type LazyChunk } from "@/components/content/lazy-chunk";
import type { ChatMarkdownProps, ChatMarkdownRenderer } from "@/components/content/markdown-renderer";
import { cn } from "@/lib/format/utils";
import { MIN_W_0 } from "@/lib/layout/layout";

type Renderer = typeof ChatMarkdownRenderer;

const defaultRendererChunk: LazyChunk<Renderer> = createLazyChunk({
  name: "chat-markdown",
  importModule: () =>
    import("@/components/content/markdown-renderer").then((module) => module.ChatMarkdownRenderer),
});

const RendererChunkContext = createContext<LazyChunk<Renderer>>(defaultRendererChunk);

export function ChatMarkdownRendererProvider({
  value,
  children,
}: {
  value: LazyChunk<Renderer>;
  children: ReactNode;
}) {
  return <RendererChunkContext.Provider value={value}>{children}</RendererChunkContext.Provider>;
}

export function useChatMarkdownRenderer(): LazyChunk<Renderer> {
  return useContext(RendererChunkContext);
}

export function preloadChatMarkdown(): Promise<void> {
  return defaultRendererChunk.preload();
}

if (typeof window !== "undefined") {
  void preloadChatMarkdown();
}

export const ChatMarkdown = memo(function ChatMarkdown(props: ChatMarkdownProps) {
  const rendererChunk = useChatMarkdownRenderer();
  const [Renderer, setRenderer] = useState<Renderer | null>(() => rendererChunk.peek());

  useEffect(() => {
    if (Renderer !== null) return;
    let active = true;
    void rendererChunk.load().then(
      (loaded) => {
        if (active) setRenderer(() => loaded);
      },
      () => undefined,
    );
    return () => {
      active = false;
    };
  }, [Renderer, rendererChunk]);

  if (Renderer === null) return <PlainChatMarkdown content={props.content} inline={props.inline} />;
  return <Renderer {...props} />;
});

function PlainChatMarkdown({ content, inline }: { content: string; inline?: boolean }) {
  if (inline) {
    return <span className={cn("chat-md font-sans whitespace-pre-wrap [overflow-wrap:anywhere]", MIN_W_0)} data-testid="chat-markdown-plain">{content}</span>;
  }
  return (
    <div className={cn("chat-md font-sans", MIN_W_0)} data-testid="chat-markdown-plain">
      <p className="whitespace-pre-wrap [overflow-wrap:anywhere]">{content}</p>
    </div>
  );
}
