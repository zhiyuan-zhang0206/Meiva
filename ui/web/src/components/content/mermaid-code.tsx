// meiva:note
// Mermaid loads only for fenced diagrams. A provider owns the lazy renderer seam so tests
// can supply SVG without browser layout. Serialize initialization with rendering because
// Mermaid's configuration is shared; each attempt removes its own measurement/error host.
// The trusted response nonce reaches Mermaid's measurement host before layout (the pinned
// package patch forwards it to the generated style). Reapply it only to that generated root
// style after sanitization: browser serialization hides connected elements' nonce attributes.
// Streaming edits, failed imports and invalid diagrams retain copyable source.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.md
"use client";

import { createContext, useContext, useEffect, useId, useState, type ReactNode } from "react";
import { useTheme } from "next-themes";
import { useNonce } from "@/lib/auth/nonce-context";
import { CopyButton } from "./copy-button";
import { createLazyChunk, type LazyChunk } from "./lazy-chunk";

export type MermaidRenderer = (id: string, code: string, dark: boolean, nonce?: string) => Promise<string>;

const defaultChunk = createLazyChunk<MermaidRenderer>({
  name: "mermaid",
  importModule: async () => {
    const { default: mermaid } = await import("mermaid");
    let queue: Promise<unknown> = Promise.resolve();
    return (id, code, dark, nonce) => {
      const attempt = queue.then(async () => {
        const host = document.createElement("div");
        host.nonce = nonce ?? "";
        document.body.append(host);
        try {
          mermaid.initialize({ startOnLoad: false, securityLevel: "strict",
            theme: dark ? "dark" : "default", suppressErrorRendering: true });
          const { svg } = await mermaid.render(id, code, host);
          const output = document.createElement("div");
          output.innerHTML = svg;
          const style = output.querySelector("svg > style");
          if (nonce && style) style.setAttribute("nonce", nonce);
          return output.innerHTML;
        } finally {
          host.remove();
        }
      });
      queue = attempt.catch(() => undefined);
      return attempt;
    };
  },
});

const RendererContext = createContext<LazyChunk<MermaidRenderer>>(defaultChunk);

export function MermaidRendererProvider({ value, children }: {
  value: LazyChunk<MermaidRenderer>; children: ReactNode;
}) {
  return <RendererContext.Provider value={value}>{children}</RendererContext.Provider>;
}

export function useMermaidRenderer() {
  return useContext(RendererContext);
}

export function MermaidCode({ code, className }: { code: string; className?: string }) {
  const renderer = useMermaidRenderer();
  const nonce = useNonce();
  const { resolvedTheme } = useTheme();
  const dark = resolvedTheme === "dark";
  const id = `mermaid-${useId().replace(/[^a-zA-Z0-9-]/g, "")}`;
  const [output, setOutput] = useState<{ code: string; dark: boolean; nonce?: string; svg: string } | null>(null);

  useEffect(() => {
    let active = true;
    void renderer.load().then((render) => render(id, code, dark, nonce)).then(
      (svg) => { if (active) setOutput({ code, dark, nonce, svg }); },
      () => { if (active) setOutput(null); },
    );
    return () => { active = false; };
  }, [renderer, id, code, dark, nonce]);

  return (
    <div className="group relative">
      <CopyButton text={code} label="mermaid code" />
      {output?.code === code && output.dark === dark && output.nonce === nonce ? (
        <div className="overflow-x-auto" dangerouslySetInnerHTML={{ __html: output.svg }} />
      ) : (
        <pre><code className={className}>{code}</code></pre>
      )}
    </div>
  );
}
