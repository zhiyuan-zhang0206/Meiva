"use client";

// meiva:note
// Per-item body renderer (ItemView). Human user messages render inside a clean
// right-aligned bubble with its display-ready payload,
// Meiva's agent_chat replies render as direct unboxed prose on the canvas, and
// secondary steps (thinking, code, output, inter-agent messages, notes) render
// inside collapsible TurnBlocks / the Live Activity Window through `./card` and
// `./row`.
// meiva:link ui/web/src/docs/frontend-components/timeline-view/card-chrome.md
// meiva:note
//
// -- Color mapping -- lives entirely in `./card`:messageCardConfig. ItemView
// carries no per-kind border / background; it just emits the inner content
// (markdown / code / envelope / marker payload).
//
// system_marker renders inside a collapsible MessageCard for every non-silent
// family: lifecycle / memory / note render Markdown bodies,
// background_task / system_notice strip a leading status: prefix into
// MarkerBody, error renders ErrorMarkerBody, and unrecognized renders
// UnknownMarkerChip.

import { FileText } from "lucide-react";
import { useTranslations } from "next-intl";
import { Fragment, memo, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { ChatMarkdown } from "@/components/content/markdown";
import { PythonCode } from "@/components/content/python-code";
import { Spinner } from "@/components/ui/spinner";
import { assetUrl } from "@/lib/transport/api";
import type { BackendTimelineItem, TimelineUploadFile } from "@/lib/contracts/types";
import { SSE_EVENT_WINDOW_MS } from "@/lib/contracts/constants-generated";
import {
  getCachedImageDimensions,
  normalizeImageDimensions,
  setCachedImageDimensions,
  type ImageDimensions,
} from "@/lib/timeline/image-dimensions-cache";
import { repairStreamingMarkdown } from "@/lib/markdown/streaming-repair";
import { useMediaQuery } from "@/lib/layout/use-media-query";
import { domHighlightHost } from "@/lib/timeline/reveal/edge-fade";
import { useStreamEdgeFade } from "@/lib/timeline/reveal/use-stream-edge-fade";
import { useStreamReveal } from "@/lib/timeline/reveal/use-stream-reveal";
import { cn, formatFileSize, isRasterImageType } from "@/lib/format/utils";

import { TimelineItemImageBatch } from "./model/image-loading";
import { ErrorMarkerBody, MarkerBody, UnknownMarkerChip } from "./markers";
import { classifyMarker, isSilentMarker, stripStatusPrefix } from "./model/marker-classify";
import { inboundKind } from "./model/runs";
import { isLiveReasoning } from "./model/reasoning-clock";
import { FLEX, OVERFLOW_HIDDEN } from "@/lib/layout/layout";

// Cap how often a streaming item re-parses its content (markdown / Prism).
// One re-parse per SSE event window (~25 FPS) reads as live while removing the
// per-chunk parse storm that pegs the mobile main thread. The window is the
// same constant the agent-side publisher coalesces into (generated from
// base/events/live/projection.py EVENT_COALESCE_MS). The reveal hook (use-stream-reveal.ts) commits to React no more often than this.
//
// A FIXED window is not enough: the parse is O(payload length), so as a code
// block streams, each flush re-highlights the whole accumulated text — total
// work is quadratic and the main thread saturates (user report 2026-09-06; bench: a 33-line
// block ≈ 69ms/highlight ≈ 1.7s of main-thread work per streaming second at
// 25Hz, a 300-line block ≈ 11.8s/s). The window therefore widens linearly with
// payload length — one extra SSE window per 2400 bytes, capped at 1s — so a
// 300-line block re-parses ~1x/s while chat/reasoning keep the live 40ms
// cadence. Once the stream has settled and the reveal has flushed (<=250ms), the
// final text renders in full.
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

// meiva:note
// Timeline payloads are display-ready; Copy must preserve literal headers and paragraphs.
export function copyPayloadFor(item: BackendTimelineItem): string {
  return item.payload;
}

const THUMBNAIL_MAX_WIDTH = 256;
const THUMBNAIL_MAX_HEIGHT = 192;

function computeThumbnailBox(dims: ImageDimensions): ImageDimensions {
  const scale = Math.min(1, THUMBNAIL_MAX_WIDTH / dims.width, THUMBNAIL_MAX_HEIGHT / dims.height);
  return {
    width: Math.max(1, Math.round(dims.width * scale)),
    height: Math.max(1, Math.round(dims.height * scale)),
  };
}

// meiva:note
// Attach items (attachMode) render through AttachContent: the backend caption
// lines interleave with their thumbnails (label → image → label → image), the
// leading notice line renders dimmed, and every thumbnail opens a lightbox on
// click instead of navigating away. Multimodal inbound_chat shares the same
// lightbox thumbnails.
// meiva:link agent/messages/docs/message-format.md
function Thumbnail({
  src,
  alt,
  dimensions,
  onError,
}: {
  src: string;
  alt: string;
  dimensions?: { readonly width: number; readonly height: number } | null;
  onError?: () => void;
}) {
  // Click-to-zoom overlay: no navigation (user ruling 2026-08-27 — the old
  // <a target="_blank"> opened the raw data-URI in a new tab). Click anywhere
  // on the backdrop (or the image) or press Escape to close. While open the
  // dialog holds focus (Tab is trapped — the dialog is the only focusable
  // node), background scroll is locked, and closing returns focus to the
  // trigger button (QA review #831 nit).
  const [open, setOpen] = useState(false);
  const [lockedLayout, setLockedLayout] = useState<{ src: string; dims: ImageDimensions | null }>(
    () => ({
      src,
      dims: normalizeImageDimensions(dimensions) ?? getCachedImageDimensions(src),
    }),
  );
  const [loadedSrc, setLoadedSrc] = useState<string | null>(null);
  const [failedSrc, setFailedSrc] = useState<string | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);

  let resolvedDims = lockedLayout.dims;
  if (lockedLayout.src !== src) {
    resolvedDims = normalizeImageDimensions(dimensions) ?? getCachedImageDimensions(src);
    setLockedLayout({ src, dims: resolvedDims });
  }

  const loaded = loadedSrc === src;
  const failed = failedSrc === src;

  const attachImgRef = useCallback(
    (node: HTMLImageElement | null) => {
      imgRef.current = node;
      if (node && node.complete && node.naturalWidth > 0 && node.naturalHeight > 0) {
        setCachedImageDimensions(src, node.naturalWidth, node.naturalHeight);
        setLoadedSrc((prev) => (prev === src ? prev : src));
      }
    },
    [src],
  );

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
        // meiva:note
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

  // meiva:note
  // Lock the thumbnail frame on first paint so network decode causes 0px vertical
  // shift: known dimensions (server metadata or session cache) reserve the exact
  // scaled aspect box; legacy uploads without dimensions stay inside a fixed
  // 256x192 box for the current mount while caching natural dimensions for later mounts.
  const box = resolvedDims ? computeThumbnailBox(resolvedDims) : null;
  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-label={alt}
        data-testid="attach-thumbnail"
        style={
          box && resolvedDims
            ? { width: `${box.width}px`, aspectRatio: `${resolvedDims.width} / ${resolvedDims.height}` }
            : undefined
        }
        className={
          box
            ? cn(
                "relative block max-w-[min(16rem,100%)] max-h-48 self-start rounded border border-border cursor-zoom-in p-0",
                !loaded && !failed && "bg-muted/30",
                OVERFLOW_HIDDEN,
              )
            : cn(
                "relative block h-48 w-64 max-w-[min(16rem,100%)] self-start rounded border border-border cursor-zoom-in p-0",
                !loaded && !failed && "bg-muted/25",
                OVERFLOW_HIDDEN,
              )
        }
      >
        {!loaded && !failed ? (
          <span
            data-testid="timeline-image-placeholder"
            aria-hidden="true"
            className={cn("pointer-events-none absolute inset-0 items-center justify-center", FLEX)}
          >
            <Spinner className="size-4" />
          </span>
        ) : null}
        {/* eslint-disable-next-line @next/next/no-img-element -- user upload / attach media, not a static asset */}
        <img
          ref={attachImgRef}
          src={src}
          alt={alt}
          width={resolvedDims?.width}
          height={resolvedDims?.height}
          crossOrigin="use-credentials"
          loading="eager"
          onLoad={(event) => {
            const el = event.currentTarget;
            if (el.naturalWidth > 0 && el.naturalHeight > 0) {
              setCachedImageDimensions(src, el.naturalWidth, el.naturalHeight);
            }
            setLoadedSrc(src);
          }}
          onError={() => {
            setFailedSrc(src);
            onError?.();
          }}
          className={cn(
            "block h-full w-full object-contain transition-opacity duration-150",
            loaded ? "opacity-100" : "opacity-0",
          )}
        />
      </button>
      {open
        ? createPortal(
            // meiva:note
            // Full-page image viewer (user ruling 2026-08-30, QA #1085): the
            // overlay must cover the whole viewport and sit above every
            // interactive control, and the enlarged image takes 60-80% of the
            // screen (big images scale down to fit, small ones keep their
            // natural size — contain, never stretched). It is mounted at
            // document.body (same pattern the Radix dialogs use), outside the
            // timeline's scroll container and its stacking contexts: inline,
            // the backdrop was once clipped to the row's box (~440x861 vs the
            // 1182x839 viewport) and the z-30 scroll-to-bottom button painted
            // above the z-50 backdrop.
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
// meiva:link agent/messages/docs/message-format.md
function AttachContent({
  payload,
  images,
  imageDimensions,
  imageCaptions,
}: {
  payload: string;
  images: string[] | null;
  imageDimensions: (ImageDimensions | null)[] | null;
  imageCaptions: string[] | null;
}) {
  const t = useTranslations("timeline");
  const lines = payload.split("\n");
  let imgIdx = 0;
  const rows = lines.map((line, i) => {
    const currentIdx = imgIdx;
    const hasImage = imageCaptions?.[currentIdx] === line;
    const src = hasImage && images != null ? images[imgIdx++] : null;
    const dims = hasImage ? (imageDimensions?.[currentIdx] ?? null) : null;
    // meiva:note
    // The backend always opens the payload with its notice line, and the envelope framing
    // is stripped before it reaches the timeline.
    // meiva:link agent/messages/docs/message-format.md
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
            <Thumbnail src={src} alt={t("attachedImage")} dimensions={dims} />
          </div>
        ) : null}
      </Fragment>
    );
  });
  // Safety net: an image whose caption never matched a line (defensive — the
  // backend contract guarantees alignment) still renders, so no thumbnail is
  // ever dropped.
  const trailingStart = imgIdx;
  const trailing = images?.slice(trailingStart) ?? [];
  return (
    <div className="space-y-1.5">
      {rows}
      {trailing.length ? (
        <div className={cn("items-start flex-wrap gap-2", FLEX)}>
          {trailing.map((src, offset) => (
            <Thumbnail
              key={src}
              src={src}
              alt={t("attachedImage")}
              dimensions={imageDimensions?.[trailingStart + offset] ?? null}
            />
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
  if (canPreviewImage && !imgFailed) {
    const dims = normalizeImageDimensions({ width: file.width, height: file.height });
    return (
      <Thumbnail
        src={resolvedUrl}
        alt={file.name}
        dimensions={dims}
        onError={() => setImgFailed(true)}
      />
    );
  }
  return (
    <div
      data-testid="timeline-upload-file"
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
  );
}

function EnvelopeContent({
  payload,
  images,
  imageDimensions = null,
  files,
  source = null,
  attachMode = false,
  codeSurface = false,
  markdown = false,
  imageCaptions = null,
  imagesFirst = false,
}: {
  payload: string;
  // meiva:note
  // Gateway-relative multimodal images resolve through assetUrl.
  images?: string[] | null;
  imageDimensions?: (ImageDimensions | null)[] | null;
  files?: TimelineUploadFile[] | null;
  source?: string | null;
  /** Attach items render their caption lines interleaved with thumbnails. */
  attachMode?: boolean;
  codeSurface?: boolean;
  /** The body is model-authored Markdown (compact summary, inter-agent message): render it through ChatMarkdown. */
  markdown?: boolean;
  /** Backend caption line per image (1:1 with `images`) — attach items only. */
  imageCaptions?: string[] | null;
  imagesFirst?: boolean;
}) {
  const t = useTranslations("timeline");
  const body = payload;
  const showBody =
    Boolean(body) &&
    !(images?.length && (body === "[image]" || body === "[files]")) &&
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
        imageDimensions={imageDimensions ?? null}
        imageCaptions={imageCaptions?.length ? imageCaptions : null}
      />
    );
  }
  const imageRow = images?.length ? (
    <div className={cn(!imagesFirst && showBody && "mt-2", "items-start flex-wrap gap-2", FLEX)}>
      {images.map((src, idx) => (
        <Thumbnail
          key={src}
          src={resolveImageSrc(src)}
          alt={t("attachedImage")}
          dimensions={imageDimensions?.[idx] ?? null}
        />
      ))}
    </div>
  ) : null;
  return (
    <>
      {imagesFirst ? imageRow : null}
      {showBody ? (
        markdown ? (
          <div className={cn(imagesFirst && images?.length && "mt-2")}>
            <ChatMarkdown content={body} />
          </div>
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
        <div className={cn("items-start flex-wrap gap-2", showBody && "mt-2", FLEX)}>
          {files.map((file, idx) => (
            <UploadedFileCard key={`${idx}:${file.url}`} file={file} />
          ))}
        </div>
      ) : null}
      {!imagesFirst ? imageRow : null}
    </>
  );
}

export const ItemView = memo(function ItemView({
  item,
  streaming,
}: {
  item: BackendTimelineItem;
  streaming: boolean;
}) {
  // Display what the reveal has uncovered, not what has arrived: the store keeps
  // every received byte, this hook paces what the reader sees (see
  // reveal/controller.ts) and throttles React commits to the markdown / Prism parse
  // budget. The hook is called unconditionally (rules of hooks); `live` is false
  // for every other kind, so the text passes through untouched.
  const live =
    (item.kind === "agent_chat" && Boolean(item.partial) && !item.interrupted) ||
    isLiveReasoning(item) ||
    (item.kind === "agent_code" && streaming);
  const { text: streamingPayload, revealing } = useStreamReveal(
    item.payload,
    live,
    streamingParseIntervalMs(item.payload.length),
  );
  const markdownPayload = useMemo(
    () => (revealing ? repairStreamingMarkdown(streamingPayload) : streamingPayload),
    [revealing, streamingPayload],
  );
  const bodyRef = useRef<HTMLDivElement>(null);
  const reducedMotion = useMediaQuery("(prefers-reduced-motion: reduce)");
  useStreamEdgeFade(bodyRef, revealing, streamingPayload, reducedMotion ? null : domHighlightHost());

  switch (item.kind) {
    case "inbound_chat": {
      return (
        <TimelineItemImageBatch>
          <EnvelopeContent
            payload={item.payload}
            images={item.images}
            imageDimensions={item.image_dimensions}
            files={item.files}
            source={item.source}
            imagesFirst={inboundKind(item.source) === "human"}
            markdown
          />
        </TimelineItemImageBatch>
      );
    }
    case "inbound_compact_summary":
      return (
        <TimelineItemImageBatch>
          <EnvelopeContent
            payload={item.payload}
            images={item.images}
            imageDimensions={item.image_dimensions}
            markdown
          />
        </TimelineItemImageBatch>
      );
    case "inbound_compact_request":
      return (
        <TimelineItemImageBatch>
          <EnvelopeContent
            payload={item.payload}
            images={item.images}
            imageDimensions={item.image_dimensions}
            markdown
          />
        </TimelineItemImageBatch>
      );

    case "attach":
      return (
        <EnvelopeContent
          payload={item.payload}
          images={item.images}
          imageDimensions={item.image_dimensions}
          attachMode
          imageCaptions={item.image_captions}
        />
      );

    case "agent_chat":
      return (
        <TimelineItemImageBatch>
          <div ref={bodyRef}>
            <ChatMarkdown content={markdownPayload} />
          </div>
        </TimelineItemImageBatch>
      );

    case "agent_code":
      return (
        <div ref={bodyRef} className="rounded-md border border-border/50 bg-muted/45 dark:bg-black/20 px-2.5 py-2">
          {item.source && item.source !== "execute_code" ? (
            <pre className="whitespace-pre-wrap [overflow-wrap:anywhere] font-mono text-[12px] leading-relaxed m-0 text-foreground/90">
              {streamingPayload}
            </pre>
          ) : (
            <PythonCode code={streamingPayload} streaming={streaming} copy={false} />
          )}
        </div>
      );

    case "agent_reasoning":
      return (
        <TimelineItemImageBatch>
          <div ref={bodyRef} className="text-[12.5px] leading-relaxed text-muted-foreground/90">
            <ChatMarkdown content={markdownPayload} />
          </div>
        </TimelineItemImageBatch>
      );

    case "code_output":
      return <EnvelopeContent payload={item.payload} codeSurface />;

    case "system_prompt":
      return (
        <TimelineItemImageBatch>
          <div className="text-[12px] leading-relaxed text-muted-foreground/90">
            <ChatMarkdown content={item.payload} />
          </div>
        </TimelineItemImageBatch>
      );

    case "system_marker": {
      if (isSilentMarker(item.source, item.payload)) return null;
      const cls = classifyMarker(item.source, item.payload);
      if (cls.kind === "error") {
        return <ErrorMarkerBody payload={item.payload} />;
      }
      if (cls.kind === "unrecognized") {
        return <UnknownMarkerChip source={item.source} payload={item.payload} showHeading={false} />;
      }
      if (cls.kind === "background_task" || cls.kind === "system_notice") {
        return <MarkerBody payload={stripStatusPrefix(item.payload)} />;
      }
      return (
        <TimelineItemImageBatch>
          <MarkerBody payload={item.payload} markdown />
        </TimelineItemImageBatch>
      );
    }
  }

  // Exhaustiveness guard: TS narrows item to never at compile time; this is the
  // runtime fallback.
  /* v8 ignore next 3 */
  const _exhaustive: never = item.kind;
  console.warn("[timeline] ItemView: unknown item kind", _exhaustive);
  return null;
},
// Custom comparator: only re-render when item reference or content changes.
// ItemView is a pure function (DOM derived from item); same item + same streaming
// -> same output, so re-renders can be skipped. This matters especially for
// code_delta streaming: previously committed items are unchanged, only the last
// streaming item payload is changing — every preceding item skips re-render.
(prev, next) =>
  prev.item === next.item && prev.streaming === next.streaming,
);
