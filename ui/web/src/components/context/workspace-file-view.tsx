// meiva:note
// Workspace file viewer in SharedContextPanel for source code, Markdown, images, video, audio and PDF:
// line range highlighting (#L10 or #L10-L20), scrolling to the first highlighted line, copy controls.
"use client";

import { useQuery } from "@tanstack/react-query";
import { Download } from "lucide-react";
import { useTranslations } from "next-intl";
import { Highlight, type PrismTheme } from "prism-react-renderer";
import { useEffect, useMemo, useRef } from "react";

import { CopyButton } from "@/components/content/copy-button";
import { isAudioExtension, isImageExtension, isVideoExtension, parseLineRange } from "@/components/content/file-path-utils";
import { ChatMarkdown } from "@/components/content/markdown";
import {
  markdownImageUrl,
  resolveWorkspaceFile,
  resolveWorkspaceReference,
} from "@/components/content/markdown-image-url";
import { ZoomableImage } from "@/components/content/zoomable-image";
import { InlineError } from "@/components/ui/inline-error";
import { Spinner } from "@/components/ui/spinner";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useWorkspaceFileStore, type WorkspaceFileTarget } from "@/lib/context/workspace-file-store";
import { cn, formatFileSize } from "@/lib/format/utils";
import { FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
import { API_BASE } from "@/lib/transport/api-base";
import { useApi } from "@/lib/transport/transport-context";

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

const EXTENSION_LANGUAGE_MAP: Record<string, string> = {
  ".py": "python",
  ".ts": "typescript",
  ".tsx": "tsx",
  ".js": "javascript",
  ".jsx": "jsx",
  ".mjs": "javascript",
  ".cjs": "javascript",
  ".json": "json",
  ".sql": "sql",
  ".sh": "bash",
  ".bash": "bash",
  ".zsh": "bash",
  ".toml": "toml",
  ".yaml": "yaml",
  ".yml": "yaml",
  ".css": "css",
  ".scss": "scss",
  ".html": "markup",
  ".htm": "markup",
  ".xml": "markup",
  ".svg": "markup",
  ".diff": "diff",
  ".patch": "diff",
  ".rs": "rust",
  ".go": "go",
};

interface WorkspaceFileViewProps {
  agentId: number;
  target: WorkspaceFileTarget;
}

// meiva:note
// An open file follows its changes on its own (user ruling 2026-11-20: no refresh button): the file
// on show is read again on this interval while the page is visible, and media are not re-read.
export const FILE_REFRESH_MS = 10_000;

// meiva:note
// One file's reading area. The panel bar above owns the tabs and controls; the path bar its name.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
export function WorkspaceFileView({ agentId, target }: WorkspaceFileViewProps) {
  const t = useTranslations("toUser");
  const client = useApi();
  const { openFile } = useWorkspaceFileStore();

  const { ownerAgentId, requestPath } = useMemo(
    () => resolveWorkspaceFile(target.path, agentId),
    [target.path, agentId],
  );

  const { data, error, isLoading, refetch } = useQuery({
    queryKey: ["workspace-file", ownerAgentId, requestPath],
    queryFn: ({ signal }) => client.getWorkspaceFile(ownerAgentId, requestPath, signal),
    refetchInterval: (query) => (query.state.data?.binary === true ? false : FILE_REFRESH_MS),
  });

  const parsedRange = useMemo(() => parseLineRange(target.lineRange), [target.lineRange]);
  const firstHighlightedLineRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (parsedRange && firstHighlightedLineRef.current) {
      firstHighlightedLineRef.current.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [parsedRange, data]);

  const fallbackName = requestPath.split("/").pop() ?? requestPath;
  const fallbackDot = fallbackName.lastIndexOf(".");
  const ext =
    data?.extension ?? (fallbackDot >= 0 ? fallbackName.slice(fallbackDot).toLowerCase() : "");
  const isMarkdown = ext === ".md" || ext === ".markdown";
  const isImage = isImageExtension(ext);
  const isVideo = isVideoExtension(ext);
  const isAudio = isAudioExtension(ext);
  const isPdf = ext === ".pdf";
  const rawUrl = `${API_BASE}/api/agents/${ownerAgentId}/files/raw?path=${encodeURIComponent(data?.path ?? requestPath)}`;

  const language = EXTENSION_LANGUAGE_MAP[ext] || "text";

  return (
    <section
      data-testid="workspace-file-view"
      className={cn(FLEX, MIN_H_0, FLEX_COL, FLEX_1)}
      aria-label={target.path}
    >
      <ScrollArea className={cn(MIN_H_0, FLEX_1)}>
        {isLoading ? (
          <div className={cn("items-center justify-center px-8 py-20", FLEX, FLEX_1)}>
            <Spinner className="size-4" label={t("loading")} />
          </div>
        ) : error ? (
          <div className="p-6 space-y-3 text-center">
            <InlineError>{error instanceof Error ? error.message : "Failed to read file"}</InlineError>
            <div>
              <button
                type="button"
                onClick={() => void refetch()}
                className="text-xs text-primary underline hover:opacity-80"
              >
                {t("refresh")}
              </button>
            </div>
          </div>
        ) : !data ? null : isMarkdown ? (
          <div className="px-5 py-3.5 text-[13px] leading-[21px] text-foreground/90 dark:text-[#dfdcd6] [&>.chat-md>:first-child]:mt-0">
            <ChatMarkdown
              content={data.content}
              resolveImageUrl={(src) =>
                markdownImageUrl(
                  resolveWorkspaceReference(target.path, src),
                  ownerAgentId,
                  API_BASE,
                )
              }
              onOpenFile={(nextPath, nextLines) =>
                openFile({
                  agentId,
                  path: resolveWorkspaceReference(target.path, nextPath),
                  lineRange: nextLines,
                })
              }
            />
          </div>
        ) : isImage ? (
          <div className={cn("items-center justify-center p-6", FLEX, FLEX_COL)}>
            <ZoomableImage
              src={rawUrl}
              alt={data.name}
              className="max-w-full max-h-[70vh] rounded border shadow-sm object-contain"
            />
          </div>
        ) : isVideo ? (
          <div className={cn("items-center justify-center p-6", FLEX, FLEX_COL)}>
            <video
              controls
              preload="metadata"
              src={rawUrl}
              data-testid="workspace-video-player"
              className="max-w-full max-h-[70vh] rounded border shadow-sm"
            >
              <track kind="captions" />
            </video>
          </div>
        ) : isAudio ? (
          <div className={cn("items-center justify-center p-6", FLEX, FLEX_COL)}>
            <audio controls preload="metadata" src={rawUrl} data-testid="workspace-audio-player" aria-label={t("filesAudio")} className="w-full max-w-xl">
              <track kind="captions" />
            </audio>
          </div>
        ) : isPdf ? (
          // meiva:note
          // The browser's own PDF viewer, in a frame the gateway serves under its passive policy.
          <iframe src={rawUrl} title={data.name} data-testid="workspace-pdf" className="h-[calc(100vh-8rem)] w-full border-0" />
        ) : data.binary ? (
          <div className={cn("items-center justify-center p-8 gap-3 text-center", FLEX, FLEX_COL)}>
            <div className="text-sm font-medium">{data.name}</div>
            <div className="text-xs text-muted-foreground">
              Binary file ({formatFileSize(data.size)})
            </div>
            <a
              href={rawUrl}
              target="_blank"
              rel="noopener noreferrer"
              download={data.name}
              className="px-3 py-1.5 rounded text-xs bg-primary text-primary-foreground hover:opacity-90 inline-flex items-center gap-1.5"
            >
              <Download className="size-3.5" />
              <span>Download file</span>
            </a>
          </div>
        ) : (
          <div className="group relative p-2">
            <div className="absolute right-4 top-4 z-10">
              <CopyButton text={data.content} label={data.name} />
            </div>
            <Highlight theme={theme} code={data.content} language={language}>
              {({ tokens, getTokenProps }) => (
                <div
                  data-testid="workspace-code-content"
                  className="font-mono text-[12px] leading-relaxed py-2 overflow-x-auto"
                >
                  {tokens.map((line, i) => {
                    const lineNumber = i + 1;
                    const isHighlighted =
                      parsedRange !== null &&
                      lineNumber >= parsedRange.start &&
                      lineNumber <= parsedRange.end;
                    const isFirstHighlighted =
                      parsedRange !== null && lineNumber === parsedRange.start;

                    return (
                      <div
                        key={i}
                        ref={isFirstHighlighted ? firstHighlightedLineRef : undefined}
                        data-line-number={lineNumber}
                        className={cn(
                          "items-stretch px-2 rounded-sm transition-colors",
                          FLEX,
                          isHighlighted && "bg-primary/15 border-l-2 border-primary -ml-2 pl-2",
                        )}
                      >
                        <span className="select-none pr-4 text-right text-muted-foreground/40 w-10 shrink-0 text-[11px]">
                          {lineNumber}
                        </span>
                        <span className="whitespace-pre">
                          {line.map((token, key) => (
                            <span key={key} {...getTokenProps({ token })} />
                          ))}
                        </span>
                      </div>
                    );
                  })}
                </div>
              )}
            </Highlight>
          </div>
        )}
      </ScrollArea>
    </section>
  );
}
