// meiva:note
// Workspace file viewer in SharedContextPanel for source code, markdown, and images.
// Supports breadcrumbs navigation, line range highlighting (#L10 or #L10-L20),
// auto-scrolling to the first highlighted line, copy controls, and quick return to to_user.md.
"use client";

import { useQuery } from "@tanstack/react-query";
import { ArrowLeft, Download, FileCode, FileText, Film, House, ImageIcon, Music, PanelRight, RefreshCw, ScanSearch } from "lucide-react";
import { useTranslations } from "next-intl";
import { Highlight, type PrismTheme } from "prism-react-renderer";
import { useEffect, useMemo, useRef } from "react";

import { CopyButton } from "@/components/content/copy-button";
import { isAudioExtension, isImageExtension, isVideoExtension, parseLineRange } from "@/components/content/file-path-utils";
import { ChatMarkdown } from "@/components/content/markdown";
import { ZoomableImage } from "@/components/content/zoomable-image";
import { InlineError } from "@/components/ui/inline-error";
import { Spinner } from "@/components/ui/spinner";
import { ScrollArea } from "@/components/ui/scroll-area";
import { useWorkspaceFileStore, type WorkspaceFileTarget } from "@/lib/context/workspace-file-store";
import { cn, formatFileSize } from "@/lib/format/utils";
import { COLUMN_TITLE_BAR_WRAP_CLASS, FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
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
  mobile: boolean;
  inspecting?: boolean;
  onInspect?: () => void;
  onCollapse?: () => void;
}

export function WorkspaceFileView({
  agentId,
  target,
  mobile,
  inspecting = false,
  onInspect,
  onCollapse,
}: WorkspaceFileViewProps) {
  const t = useTranslations("toUser");
  const tInspector = useTranslations("inspector");
  const client = useApi();
  const { history, closeFile, goBack, openFile } = useWorkspaceFileStore();

  const { data, error, isLoading, refetch, isFetching } = useQuery({
    queryKey: ["workspace-file", agentId, target.path],
    queryFn: ({ signal }) => client.getWorkspaceFile(agentId, target.path, signal),
  });

  const parsedRange = useMemo(() => parseLineRange(target.lineRange), [target.lineRange]);
  const firstHighlightedLineRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (parsedRange && firstHighlightedLineRef.current) {
      firstHighlightedLineRef.current.scrollIntoView({ block: "center", behavior: "smooth" });
    }
  }, [parsedRange, data]);

  const ext = data?.extension ?? target.path.slice(target.path.lastIndexOf(".")).toLowerCase();
  const isMarkdown = ext === ".md" || ext === ".markdown";
  const isImage = isImageExtension(ext);
  const isVideo = isVideoExtension(ext);
  const isAudio = isAudioExtension(ext);
  const rawUrl = `${API_BASE}/api/agents/${agentId}/files/raw?path=${encodeURIComponent(data?.path ?? target.path)}`;

  const language = EXTENSION_LANGUAGE_MAP[ext] || "text";

  const inspectorToggleClass = cn("rounded p-1", inspecting && "bg-foreground/20 text-foreground");

  return (
    <section
      data-testid="workspace-file-view"
      className={cn(FLEX, MIN_H_0, FLEX_COL, FLEX_1)}
      aria-label={target.path}
    >
      <div
        data-testid="workspace-file-bar"
        className={cn(
          "shrink-0 flex-wrap items-center gap-x-2 gap-y-1 px-3 py-1 text-[11px] text-muted-foreground border-b",
          COLUMN_TITLE_BAR_WRAP_CLASS,
          FLEX,
        )}
      >
        <button
          title="Return to to_user.md"
          aria-label="Return to to_user.md"
          className="rounded p-1 hover:bg-accent"
          onClick={closeFile}
        >
          <House className="size-3.5" />
        </button>
        <button
          title={history.length > 0 ? "Back" : "Return to to_user.md"}
          aria-label={history.length > 0 ? "Back" : "Return to to_user.md"}
          className="rounded p-1 hover:bg-accent"
          onClick={goBack}
        >
          <ArrowLeft className="size-3.5" />
        </button>

        <div className={cn("items-center gap-1.5 font-mono text-foreground font-medium", FLEX)}>
          {isImage ? (
            <ImageIcon className="size-3.5 opacity-70" />
          ) : isVideo ? (
            <Film className="size-3.5 opacity-70" />
          ) : isAudio ? (
            <Music className="size-3.5 opacity-70" />
          ) : isMarkdown ? (
            <FileText className="size-3.5 opacity-70" />
          ) : (
            <FileCode className="size-3.5 opacity-70" />
          )}
          <span className="truncate max-w-[200px]" title={target.path}>
            {data?.name ?? target.path.split("/").pop()}
          </span>
          {target.lineRange && (
            <span
              data-testid="file-line-range-badge"
              className="px-1.5 py-0.2 rounded text-[10px] font-mono bg-primary/20 text-primary font-normal"
            >
              #L{target.lineRange}
            </span>
          )}
        </div>

        <div className={cn("shrink-0 items-center gap-1.5 ml-1", FLEX)}>
          {data?.size !== undefined && (
            <span className="text-[10px] text-muted-foreground">({formatFileSize(data.size)})</span>
          )}
          <button
            title={t("refresh")}
            aria-label={t("refresh")}
            onClick={() => void refetch()}
            disabled={isFetching}
            className="rounded p-1 hover:bg-accent disabled:opacity-50"
          >
            <RefreshCw className={cn("size-3.5", isFetching && "animate-spin")} />
          </button>
        </div>

        <div className={cn("ml-auto shrink-0 items-center", FLEX)}>
          {onInspect ? (
            <button
              type="button"
              data-testid="open-inspector"
              title={inspecting ? tInspector("closeInspector") : tInspector("openInspector")}
              aria-label={inspecting ? tInspector("closeInspector") : tInspector("openInspector")}
              aria-pressed={inspecting}
              onClick={onInspect}
              className={inspectorToggleClass}
            >
              <ScanSearch className="size-3.5" />
            </button>
          ) : null}
          {onCollapse && !mobile ? (
            <button
              type="button"
              data-testid="context-panel-collapse"
              title={t("hideContextPanel")}
              aria-label={t("hideContextPanel")}
              onClick={onCollapse}
              className="rounded p-1 hover:bg-accent"
            >
              <PanelRight className="size-3.5" />
            </button>
          ) : null}
        </div>
      </div>

      <ScrollArea className={cn(MIN_H_0, FLEX_1)}>
        {isLoading ? (
          <div className={cn("items-center justify-center p-8", FLEX, FLEX_1)}>
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
              onOpenFile={(nextPath, nextLines) =>
                openFile({ agentId, path: nextPath, lineRange: nextLines })
              }
            />
          </div>
        ) : isImage ? (
          <div className={cn("items-center justify-center p-6 gap-3", FLEX, FLEX_COL)}>
            <ZoomableImage
              src={rawUrl}
              alt={data.name}
              className="max-w-full max-h-[70vh] rounded border shadow-sm object-contain"
            />
            <span className="text-xs text-muted-foreground">
              {data.name} ({formatFileSize(data.size)})
            </span>
          </div>
        ) : isVideo ? (
          <div className={cn("items-center justify-center p-6 gap-3", FLEX, FLEX_COL)}>
            <video
              controls
              preload="metadata"
              src={rawUrl}
              data-testid="workspace-video-player"
              className="max-w-full max-h-[70vh] rounded border shadow-sm"
            >
              <track kind="captions" />
            </video>
            <span className="text-xs text-muted-foreground">
              {data.name} ({formatFileSize(data.size)})
            </span>
          </div>
        ) : isAudio ? (
          <div className={cn("items-center justify-center p-6 gap-3", FLEX, FLEX_COL)}>
            <audio
              controls
              preload="metadata"
              src={rawUrl}
              data-testid="workspace-audio-player"
              className="w-full max-w-md"
            >
              <track kind="captions" />
            </audio>
            <span className="text-xs text-muted-foreground">
              {data.name} ({formatFileSize(data.size)})
            </span>
          </div>
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
