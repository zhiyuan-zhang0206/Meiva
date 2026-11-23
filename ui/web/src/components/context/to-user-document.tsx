"use client";

import { useTranslations } from "next-intl";

import { ChatMarkdown } from "@/components/content/markdown";
import { isFilePath } from "@/components/content/file-path-utils";
import { useWorkspaceFileStore } from "@/lib/context/workspace-file-store";
import { InlineError } from "@/components/ui/inline-error";
import { Spinner } from "@/components/ui/spinner";
import { ScrollArea } from "@/components/ui/scroll-area";
import { ZoomableImage } from "@/components/content/zoomable-image";
import type { OngoingDocument, ToUserDocument } from "@/lib/contracts/types";
import { fileQuery } from "@/lib/context/reference-url";
import { cn } from "@/lib/format/utils";
import { BAR_DIVIDER_CLASS, FLEX, FLEX_1, FLEX_COL, MIN_H_0 } from "@/lib/layout/layout";
import { API_BASE } from "@/lib/transport/api-base";


type RefusalText = "unavailable" | "tooLarge" | "missing" | "unreadable";

// meiva:note
// The panel's words follow the response's own fields, never the status and never the
// error's class: a refusal is permanent for this document version, so only an answer
// the server called retryable may ask for a retry, and a read that never got an answer
// at all is the one other case where retrying is honest advice.
// meiva:link ui/web/src/docs/frontend-state/api-requests.md
function refusalOf(error: unknown): { text: RefusalText; raw: boolean } {
  if (typeof error !== "object" || error === null || !("status" in error) || typeof error.status !== "number") {
    return { text: "unavailable", raw: false };
  }
  const code = "code" in error && typeof error.code === "string" ? error.code : null;
  if (code === "to_user_document_too_large") return { text: "tooLarge", raw: true };
  if (code === "to_user_reference_gone") return { text: "missing", raw: false };
  if (code === "to_user_document_unreadable") return { text: "unreadable", raw: true };
  return "retryable" in error && error.retryable === true
    ? { text: "unavailable", raw: false }
    : { text: "unreadable", raw: true };
}

// meiva:note
// The conversation's to-user document: the panel's first tab, and only its reading area. The panel
// bar above it owns the tabs and controls, and the path bar its name and update time. A link to
// another file of the workspace opens that file as a tab (Markdown included), so every file is a
// browser history entry and the document itself is the one place Home leads to.
// meiva:link ui/web/src/docs/frontend-state/shared-context/shared-context.md
export function ToUserDocumentView({ agentId, document, failure, pending, bodyHidden = false, viewportRef, ongoing = false }: {
  agentId: number;
  document: OngoingDocument | ToUserDocument | undefined;
  ongoing?: boolean;
  failure: unknown;
  pending: boolean;
  bodyHidden?: boolean;
  viewportRef?: React.RefObject<HTMLDivElement | null>;
}) {
  const t = useTranslations("toUser");
  const current = document;
  const loading = pending;
  const failed = failure != null;
  const refusal = failed ? refusalOf(failure) : null;
  const content = current?.content;
  const resources = new Map(Object.entries(current && "resources" in current ? current.resources ?? {} : {}).map(([href, resource]) => {
    const version = resource.version ? `&v=${resource.version}` : "";
    const fragment = href.includes("#") ? href.slice(href.indexOf("#")) : "";
    return [href, { ...resource, url: `${API_BASE}/api/agents/${agentId}/to-user/resource?${fileQuery([resource.file])}${version}${fragment}` }];
  }));
  const byUrl = new Map([...resources.values()].map((resource) => [resource.url, resource]));
  return (
    <section className={cn(FLEX, MIN_H_0, FLEX_COL, bodyHidden ? "shrink-0" : FLEX_1)} aria-label={t(ongoing ? "ongoing" : "sharedContext")}>
      {!bodyHidden && current && refusal ? (
        <InlineError data-testid="document-stale" className={cn("relative shrink-0 px-5 py-2", BAR_DIVIDER_CLASS)} detail={t(refusal.text)}>
          {t("stale")}
        </InlineError>
      ) : null}
      <ScrollArea data-testid="shared-document" hidden={bodyHidden} viewportRef={viewportRef} className={cn(MIN_H_0, FLEX_1)}>
        {bodyHidden ? null : (
          <>
            {!content ? (
              <div className={cn("items-center justify-center px-8 py-20 text-center text-[13px] text-muted-foreground", FLEX, FLEX_1)}>
                {loading && !current ? <Spinner className="size-4" label={t("loading")} /> : refusal && !current ? (
                  <div data-testid="document-failure" className="space-y-1">
                    <InlineError>{t(refusal.text)}</InlineError>
                  </div>
                ) : current && "legacy_html" in current && current.legacy_html ? t("legacyHtml") : t(ongoing ? "ongoingEmpty" : "empty")}
              </div>
            ) : (
              <div data-testid="document-content" className="px-5 py-3.5 text-[13px] leading-[21px] text-foreground/90 dark:text-[#dfdcd6] [&>.chat-md>:first-child]:mt-0">
                <ChatMarkdown content={content} resolveUrl={ongoing ? undefined : (href) => {
                  const resource = resources.get(href);
                  return resource?.url ?? (/^(https?:|mailto:|#)/i.test(href) ? href : "about:blank");
                }} components={{
                  img: ({ src, alt }) => (
                    typeof src === "string"
                      ? <ZoomableImage src={src} alt={alt ?? ""} />
                      : null
                  ),
                  a: ({ href, children }) => {
                    const resource = href ? byUrl.get(href) : undefined;
                    const external = !href?.startsWith("#");
                    return <a href={href} target={external ? "_blank" : undefined} rel={external ? "noopener noreferrer" : undefined} onClick={(event) => {
                      if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey || event.button !== 0) return;
                      if (href) {
                        const fragment = href.includes("#") ? href.slice(href.indexOf("#")) : "";
                        const targetPath = resource ? `${resource.file}${fragment}` : href;
                        const fileMatch = isFilePath(targetPath);
                        if (fileMatch.isPath) {
                          event.preventDefault();
                          useWorkspaceFileStore.getState().openFile({
                            agentId,
                            path: fileMatch.path,
                            lineRange: fileMatch.lineRange,
                          });
                        }
                      }
                    }}>{children}</a>;
                  },
                }} />
              </div>
            )}
          </>
        )}
      </ScrollArea>
    </section>
  );
}
