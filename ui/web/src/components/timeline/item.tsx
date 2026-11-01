"use client";

// meiva:note
// Per-item body renderer (ItemView). Human user messages render inside a clean
// right-aligned bubble (stripping the backend "user [timestamp]:" envelope),
// Meiva's agent_chat replies render as direct unboxed prose on the canvas, and
// secondary steps (thinking, code, output, inter-agent messages, notes) render
// inside collapsible TurnBlocks / the Live Activity Window through `./card` and
// `./row`.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
//
// -- Color mapping -- lives entirely in `./card`:messageCardConfig. ItemView
// carries no per-kind border / background; it just emits the inner content
// (markdown / code / envelope / marker payload).
//
// system_marker is the one split kind: the lifecycle / memory / note families
// render their payload body here (their icon + title + color come from the card),
// while the ephemeral families (compact_done / cancelled / error / unrecognized)
// have no card and render bare through EphemeralSystemMarker.

import { FileText } from "lucide-react";
import { useTranslations } from "next-intl";
import { Fragment, memo, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { ChatMarkdown } from "@/components/content/markdown";
import { PythonCode } from "@/components/content/python-code";
import { assetUrl } from "@/lib/transport/api";
import type { BackendTimelineItem, TimelineUploadFile } from "@/lib/contracts/types";
import { SSE_EVENT_WINDOW_MS } from "@/lib/contracts/constants-generated";
import { useThrottledStreaming } from "@/lib/timeline/use-throttled-streaming";
import { cn, formatFileSize, isRasterImageType } from "@/lib/format/utils";

import { EphemeralSystemMarker, MarkerBody } from "./markers";
import { classifyMarker } from "./model/marker-classify";
import { isLiveReasoning } from "./model/reasoning-clock";
import { inboundKind } from "./model/runs";
import { FLEX } from "@/lib/layout/layout";

// Cap how often a streaming item re-parses its content (markdown / Prism).
// One re-parse per SSE event window (~25 FPS) reads as live while removing the
// per-chunk parse storm that pegs the mobile main thread. The window is the
// same constant the agent-side publisher coalesces into (generated from
// base/events/live/projection.py EVENT_COALESCE_MS). See use-throttled-streaming.ts.
//
// A FIXED window is not enough: the parse is O(payload length), so as a code
// block streams, each flush re-highlights the whole accumulated text — total
// work is quadratic and the main thread saturates (user report 2026-09-06; bench: a 33-line
// block ≈ 69ms/highlight ≈ 1.7s of main-thread work per streaming second at
// 25Hz, a 300-line block ≈ 11.8s/s). The window therefore widens linearly with
// payload length — one extra SSE window per 2400 bytes, capped at 1s — so a
// 300-line block re-parses ~1x/s while chat/reasoning keep the live 40ms
// cadence. The settle path (use-throttled-streaming.ts, !live) still renders
// the final text in full immediately.
const STREAM_PARSE_INTERVAL_BASE_MS = SSE_EVENT_WINDOW_MS;
const STREAM_PARSE_INTERVAL_MAX_MS = 1000;
const STREAM_PARSE_INTERVAL_BYTES_PER_STEP = 2400;

export function streamingParseIntervalMs(payloadLength: number): number {
  const steps = Math.ceil(payloadLength / STREAM_PARSE_INTERVAL_BYTES_PER_STEP);
  return Math.min(
    STREAM_PARSE_INTERVAL_MAX_MS,
    Math.max(STREAM_PARSE_INTERVAL_BASE_MS, steps * STREAM_PARSE_INTERVAL_BASE_MS),
  );
}

// envelope wrap is always "<header>:\n\n<body>" (envelope.py wrap_inbound /
// exec/output.py wrap_code_output both follow this). Split out the header as a metadata
// label and the body as the main content — preserve raw text, only layer
// visually. Empty header then entire payload is body.
export function splitEnvelope(payload: string): { header: string; body: string } {
  const idx = payload.indexOf("\n\n");
  if (idx > 0 && idx < 200) {
    // Cap header at 200 chars so a long stdout first paragraph is not mistaken for the header
    return { header: payload.slice(0, idx), body: payload.slice(idx + 2) };
  }
  return { header: "", body: payload };
}

// meiva:note
// Strip the backend `<header>\n\n` envelope from human user messages so Copy
// puts only the user's words on the clipboard.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.meiva.okf.md
export function copyPayloadFor(item: BackendTimelineItem): string {
  if (
    item.kind === "inbound_chat" &&
    inboundKind(item.source) === "human" &&
    !item.impersonation
  ) {
    return splitEnvelope(item.payload).body;
  }
  return item.payload;
}

// meiva:note
// Envelope content — dim single-line header + monospace body + optional image thumbnails.
// The card frame provides the colored border / background; this renders only the inner
// text, unpadded. Attach items (attachMode) render through AttachContent: the backend
// caption lines interleave with their thumbnails (label → image → label → image), the
// leading notice line renders dimmed, and every thumbnail opens a lightbox on click
// instead of navigating away. Multimodal inbound_chat keeps the envelope layout but
// shares the same lightbox thumbnails.
// meiva:link agent/messages/docs/message-format.meiva.okf.md
function Thumbnail({
  src,
  alt,
  onError,
}: {
  src: string;
  alt: string;
  onError?: () => void;
}) {
  // Click-to-zoom overlay: no navigation (user ruling 2026-08-27 — the old
  // <a target="_blank"> opened the raw data-URI in a new tab). Click anywhere
  // on the backdrop (or the image) or press Escape to close. While open the
  // dialog holds focus (Tab is trapped — the dialog is the only focusable
  // node), background scroll is locked, and closing returns focus to the
  // trigger button (QA review #831 nit).
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const prevOverflow = document.body.style.overflow;
    const trigger = buttonRef.current;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") {
        setOpen(false);
      } else if (e.key === "Tab") {
        // Focus trap: keep Tab inside the modal instead of escaping to the
        // background content behind the overlay.
        e.preventDefault();
        dialogRef.current?.focus();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = prevOverflow;
      trigger?.focus();
    };
  }, [open]);
  // Layout-shift guard (QA review-955): the button is the fixed-size layout node
  // (16rem × 12rem — exactly the max-h-48 / max-w-[16rem] cap of the content
  // below), so the row height is reserved from the first paint. The lazy <img> is
  // absolutely centered inside it, so its own size growth (2×2px while unloaded →
  // capped content size once decoded) never moves the flow — zero CLS on the
  // scroll path. Keep the button size in sync with the img caps.
  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-label={alt}
        data-testid="attach-thumbnail"
        className="relative block h-48 w-64 cursor-zoom-in rounded border-0 bg-transparent p-0"
      >
        {/* eslint-disable-next-line @next/next/no-img-element -- user upload / attach media, not a static asset */}
        <img
          src={src}
          alt={alt}
          crossOrigin="use-credentials"
          loading="lazy"
          onError={onError}
          className="absolute inset-0 m-auto max-h-48 max-w-[16rem] rounded border border-border object-contain"
        />
      </button>
      {open
        ? createPortal(
            // Full-page image viewer (user ruling 2026-08-30, QA #1085): the
            // overlay must cover the whole viewport and sit above every
            // interactive control, and the enlarged image takes 60-80% of the
            // screen (big images scale down to fit, small ones keep their
            // natural size — contain, never stretched). It cannot be rendered
            // inline inside the timeline row: `.timeline-item` carries
            // `content-visibility: auto` (globals.css) and therefore
            // paint+layout containment, which turns the row into the
            // containing block of `fixed` descendants AND a stacking context
            // — the backdrop was clipped to the row's box (~440x861 vs the
            // 1182x839 viewport) and the z-30 scroll-to-bottom button painted
            // above the z-50 backdrop. Mounting at document.body (same
            // pattern the Radix dialogs use) restores viewport coverage +
            // root stacking order.
            <div
              ref={dialogRef}
              role="dialog"
              aria-modal="true"
              aria-label={alt}
              data-testid="attach-lightbox"
              tabIndex={-1}
              onClick={() => setOpen(false)}
              className={cn("fixed inset-0 z-50 cursor-zoom-out items-center justify-center bg-black/80 p-4 outline-none", FLEX)}
            >
              {/* eslint-disable-next-line @next/next/no-img-element -- user upload / attach media, not a static asset */}
              <img
                src={src}
                alt={alt}
                crossOrigin="use-credentials"
                loading="lazy"
                className="max-h-[80vh] max-w-[80vw] rounded object-contain shadow-2xl"
              />
            </div>,
            document.body,
          )
        : null}
    </>
  );
}

// meiva:note
// Attach item body: the caption lines rendered one per row, each delivered image's
// thumbnail directly under its own line (label1 → image1 → label2 → image2 — user ruling
// 2026-08-27). Pairing is structural: `imageCaptions` holds the exact backend-generated
// caption line of each image (1:1 with `images`), so a line matches at most one image and
// skipped entries (no image, "not delivered" reason) fall through as plain rows. The
// leading notice line renders dimmed and small.
// meiva:link agent/messages/docs/message-format.meiva.okf.md
function AttachContent({
  payload,
  images,
  imageCaptions,
}: {
  payload: string;
  images: string[] | null;
  imageCaptions: string[] | null;
}) {
  const t = useTranslations("timeline");
  const lines = payload.split("\n");
  let imgIdx = 0;
  const rows = lines.map((line, i) => {
    const hasImage = imageCaptions?.[imgIdx] === line;
    const src = hasImage && images != null ? images[imgIdx++] : null;
    // meiva:note
    // The backend always opens the payload with its notice line, and the envelope framing
    // is stripped before it reaches the timeline.
    // meiva:link agent/messages/docs/message-format.meiva.okf.md
    const isNotice = i === 0;
    return (
      <Fragment key={i}>
        <div
          className={cn(
            "whitespace-pre-wrap [overflow-wrap:anywhere] font-mono leading-relaxed m-0",
            isNotice
              ? "text-[11px] text-muted-foreground/70"
              : "text-[13px] text-foreground/90",
          )}
        >
          {line}
        </div>
        {src ? (
          <div className="mt-1">
            <Thumbnail src={src} alt={t("attachedImage")} />
          </div>
        ) : null}
      </Fragment>
    );
  });
  // Safety net: an image whose caption never matched a line (defensive — the
  // backend contract guarantees alignment) still renders, so no thumbnail is
  // ever dropped.
  const trailing = images?.slice(imgIdx) ?? [];
  return (
    <div className="space-y-1.5">
      {rows}
      {trailing.length ? (
        <div className={cn("flex-wrap gap-2", FLEX)}>
          {trailing.map((src) => (
            <Thumbnail key={src} src={src} alt={t("attachedImage")} />
          ))}
        </div>
      ) : null}
    </div>
  );
}

const MAX_INLINE_IMAGE_BYTES = 8 * 1024 * 1024;

function UploadedFileCard({ file }: { file: TimelineUploadFile }) {
  const [imgFailed, setImgFailed] = useState(false);
  const canPreviewImage =
    isRasterImageType(file.content_type) && file.size <= MAX_INLINE_IMAGE_BYTES;
  const resolvedUrl = assetUrl(file.url);
  return (
    <div data-testid="timeline-upload-file" className="space-y-1.5">
      {canPreviewImage && !imgFailed ? (
        <Thumbnail
          src={resolvedUrl}
          alt={file.name}
          onError={() => setImgFailed(true)}
        />
      ) : null}
      <div
        className={cn(
          "items-center gap-2 rounded-md border border-border/70 bg-background/60 px-2.5 py-1.5 text-xs",
          FLEX,
        )}
      >
        <FileText className="size-3.5 shrink-0 text-muted-foreground" />
        <a
          href={resolvedUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="truncate font-mono font-medium text-foreground underline-offset-2 hover:underline"
        >
          {file.name}
        </a>
        <span className="shrink-0 text-[11px] text-muted-foreground">
          {formatFileSize(file.size)} · {file.content_type}
        </span>
      </div>
    </div>
  );
}

function EnvelopeContent({
  payload,
  images,
  files,
  source = null,
  attachMode = false,
  plainMessage = false,
  hideHeader = false,
  codeSurface = false,
  markdown = false,
  imageCaptions = null,
}: {
  payload: string;
  // Image reference urls on a multimodal inbound — rendered as thumbnails below
  // the text. Gateway-relative, so each is resolved through assetUrl.
  images?: string[] | null;
  files?: TimelineUploadFile[] | null;
  source?: string | null;
  /** Attach items render their caption lines interleaved with thumbnails. */
  attachMode?: boolean;
  /** Impersonation messages store the original body without an envelope. */
  plainMessage?: boolean;
  hideHeader?: boolean;
  codeSurface?: boolean;
  /** The body is model-authored Markdown (compact summary, inter-agent message): render it through ChatMarkdown. */
  markdown?: boolean;
  /** Backend caption line per image (1:1 with `images`) — attach items only. */
  imageCaptions?: string[] | null;
}) {
  const t = useTranslations("timeline");
  const { header, body } = plainMessage ? { header: "", body: payload } : splitEnvelope(payload);
  const isUploadBatchWithFiles = source === "system:upload" && Boolean(files?.length);
  const showBody =
    !isUploadBatchWithFiles &&
    Boolean(body) &&
    !(images?.length && body === "[image]") &&
    !((Boolean(files?.length) || source === "user") && body === "[files]");
  // Attach items carry data URIs (self.attach media) — those render raw;
  // gateway-relative urls (user uploads) resolve through assetUrl.
  const resolveImageSrc = (src: string) => (src.startsWith("data:") ? src : assetUrl(src));
  // Attach items render through AttachContent: per-image captions interleave
  // label → thumbnail; without captions (legacy checkpoints, caption-only
  // attaches) every image falls to the trailing row and the lines stay plain
  // rows — the old layout, minus the navigating <a> wrappers.
  if (attachMode) {
    return (
      <AttachContent
        payload={payload}
        images={images ?? null}
        imageCaptions={imageCaptions?.length ? imageCaptions : null}
      />
    );
  }
  return (
    <>
      {header && !hideHeader && !isUploadBatchWithFiles ? (
        <div className="mb-1">
          <span className="font-mono text-[11px] leading-relaxed text-muted-foreground/75 [overflow-wrap:anywhere]">
            {header}
          </span>
        </div>
      ) : null}
      {showBody ? (
        plainMessage || markdown ? (
          <ChatMarkdown content={body} />
        ) : (
          <pre
            className={cn(
              "whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] leading-relaxed text-foreground/85 m-0",
              codeSurface && "rounded-md border border-border/50 bg-muted/45 dark:bg-black/20 px-2.5 py-2",
            )}
          >
            {body}
          </pre>
        )
      ) : null}
      {files?.length ? (
        <div className={cn("flex-wrap gap-2", showBody && "mt-2", FLEX)}>
          {files.map((file, idx) => (
            <UploadedFileCard key={`${idx}:${file.url}`} file={file} />
          ))}
        </div>
      ) : null}
      {images?.length ? (
        <div className={cn("mt-2 flex-wrap gap-2", FLEX)}>
          {images.map((src) => (
            <Thumbnail key={src} src={resolveImageSrc(src)} alt={t("attachedImage")} />
          ))}
        </div>
      ) : null}
    </>
  );
}

// Shown at the tail of partial + interrupted items to distinguish "the message
// simply ends here" vs "streaming interrupted; content may be incomplete".
// ConnectionNotice pops up at the same time, but the banner is in a separate UI
// region from timeline content; this inline hint makes the cause-and-effect visible.
function InterruptedNotice() {
  const t = useTranslations("timeline");
  return (
    <div className="mt-1.5 text-[11px] text-amber-700 dark:text-amber-400 font-mono select-none">
      {t("streamingInterrupted")}
    </div>
  );
}

function StreamingCursor() {
  return (
    <span
      aria-hidden="true"
      data-testid="streaming-cursor"
      className="mt-1.5 inline-block size-2 rounded-full bg-foreground/60 animate-pulse motion-reduce:animate-none"
    />
  );
}

export const ItemView = memo(function ItemView({
  item,
  streaming,
}: {
  item: BackendTimelineItem;
  streaming: boolean;
}) {
  const t = useTranslations("timeline");
  // Throttle the heavy-parse content (chat markdown / python highlight) while it
  // streams. The hook is called unconditionally (rules of hooks); `live` is
  // false for every other kind, making it a no-op pass-through.
  const live =
    (item.kind === "agent_chat" && Boolean(item.partial)) ||
    isLiveReasoning(item) ||
    (item.kind === "agent_code" && streaming);
  const streamingPayload = useThrottledStreaming(
    item.payload,
    live,
    streamingParseIntervalMs(item.payload.length),
  );

  switch (item.kind) {
    case "inbound_chat": {
      const isHumanUser = inboundKind(item.source) === "human";
      return (
        <EnvelopeContent
          payload={item.payload}
          images={item.images}
          files={item.files}
          source={item.source}
          plainMessage={Boolean(item.impersonation)}
          hideHeader={
            (isHumanUser && item.source === "user") || item.source === "system:upload"
          }
          markdown
        />
      );
    }
    case "inbound_compact_summary":
      return <EnvelopeContent payload={item.payload} images={item.images} markdown />;
    case "inbound_compact_request":
      return <EnvelopeContent payload={item.payload} images={item.images} markdown />;

    case "attach":
      return (
        <EnvelopeContent
          payload={item.payload}
          images={item.images}
          attachMode
          imageCaptions={item.image_captions}
        />
      );

    case "agent_chat":
      return (
        <>
          <div
            className={cn(
              item.interrupted && "border-b border-dashed border-amber-500/60 pb-1",
            )}
          >
            <ChatMarkdown content={streamingPayload} />
            {item.partial && !item.interrupted ? <StreamingCursor /> : null}
          </div>
          {item.interrupted ? <InterruptedNotice /> : null}
        </>
      );

    case "agent_code":
      return (
        <>
          <div
            className={cn(
              "rounded-md border border-border/50 bg-muted/45 dark:bg-black/20 px-2.5 py-2",
              item.interrupted && "border-amber-500/50",
            )}
          >
            {item.source && item.source !== "execute_code" ? (
              <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] leading-relaxed m-0 text-foreground/90">
                {streamingPayload}
              </pre>
            ) : (
              <PythonCode code={streamingPayload} streaming={streaming} copy={false} />
            )}
          </div>
          {item.interrupted ? <InterruptedNotice /> : null}
        </>
      );

    case "agent_reasoning":
      return (
        <>
          <div
            className={cn(
              "text-[12.5px] leading-relaxed text-muted-foreground/90",
              item.interrupted && "border-b border-dashed border-amber-500/60 pb-1",
            )}
          >
            <ChatMarkdown content={streamingPayload} />
          </div>
          {item.interrupted ? <InterruptedNotice /> : null}
        </>
      );

    case "code_output":
      return <EnvelopeContent payload={item.payload} codeSurface />;

    case "system_prompt":
      return (
        <div className="text-[12px] leading-relaxed text-muted-foreground/90">
          <ChatMarkdown content={item.payload} />
        </div>
      );

    case "system_marker": {
      // Card marker families render their payload body here; the ephemeral
      // families have no card and render their own bare output.
      const cls = classifyMarker(item.source);
      if (cls.kind === "ephemeral") {
        return <EphemeralSystemMarker source={item.source} payload={item.payload} />;
      }
      return <MarkerBody payload={item.payload} markdown />;
    }
  }

  // Exhaustiveness guard: TS narrows item to never at compile time; this is the
  // runtime fallback.
  /* v8 ignore next 3 */
  const _exhaustive: never = item.kind;
  console.warn("[timeline] ItemView: unknown item kind", _exhaustive);
  return (
    <div className="text-muted-foreground font-mono text-xs">{t("unknownEventType")}</div>
  );
},
// Custom comparator: only re-render when item reference or content changes.
// ItemView is a pure function (DOM derived from item); same item + same streaming
// -> same output, so re-renders can be skipped. This matters especially for
// code_delta streaming: previously committed items are unchanged, only the last
// streaming item payload is changing — every preceding item skips re-render.
(prev, next) =>
  prev.item === next.item && prev.streaming === next.streaming,
);
