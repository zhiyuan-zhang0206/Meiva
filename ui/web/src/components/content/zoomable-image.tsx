"use client";

import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ComponentPropsWithoutRef, ReactNode, RefObject } from "react";
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  useSyncExternalStore,
} from "react";
import { createPortal } from "react-dom";

import { Spinner } from "@/components/ui/spinner";
import { cn } from "@/lib/format/utils";
import { FLEX, OVERFLOW_HIDDEN } from "@/lib/layout/layout";
import {
  getCachedImageDimensions,
  normalizeImageDimensions,
  setCachedImageDimensions,
  type ImageDimensions,
} from "@/lib/timeline/image-dimensions-cache";

export interface ImageGateSnapshot {
  readonly hasPending: boolean;
  readonly timedOut: boolean;
  readonly initialRevealed: boolean;
}

// meiva:note
// Tracks unknown-dimension Markdown images in flight for either a timeline's initial
// cold-load reveal or a single message's post-reveal atomic batch. Subscribers only
// re-render when `hasPending`, `timedOut`, or `initialRevealed` transitions, and a
// bounded safety timeout guarantees a broken or hung URL never blocks rendering.
export class ImageGateStore {
  private readonly pending = new Set<string>();
  private readonly listeners = new Set<() => void>();
  private timer: ReturnType<typeof setTimeout> | null = null;
  private snap: ImageGateSnapshot = { hasPending: false, timedOut: false, initialRevealed: false };

  constructor(private readonly timeoutMs: number) {}

  subscribe = (listener: () => void): (() => void) => {
    this.listeners.add(listener);
    return () => {
      this.listeners.delete(listener);
    };
  };

  getSnapshot = (): ImageGateSnapshot => this.snap;

  register(id: string): () => void {
    const wasEmpty = this.pending.size === 0;
    this.pending.add(id);
    if (wasEmpty) {
      this.startTimer();
      this.publish(true, false, this.snap.initialRevealed);
    }
    return () => {
      this.settle(id);
    };
  }

  settle(id: string): void {
    if (!this.pending.delete(id)) return;
    if (this.pending.size === 0) {
      this.clearTimer();
      this.publish(false, false, this.snap.initialRevealed);
    }
  }

  markInitialRevealed(): void {
    if (this.snap.initialRevealed) return;
    this.publish(this.snap.hasPending, this.snap.timedOut, true);
  }

  dispose(): void {
    this.clearTimer();
    this.pending.clear();
    this.listeners.clear();
  }

  private startTimer(): void {
    this.clearTimer();
    this.timer = setTimeout(() => {
      this.timer = null;
      if (this.pending.size > 0) {
        this.publish(true, true, this.snap.initialRevealed);
      }
    }, this.timeoutMs);
  }

  private clearTimer(): void {
    if (this.timer !== null) {
      clearTimeout(this.timer);
      this.timer = null;
    }
  }

  private publish(hasPending: boolean, timedOut: boolean, initialRevealed: boolean): void {
    if (
      this.snap.hasPending === hasPending &&
      this.snap.timedOut === timedOut &&
      this.snap.initialRevealed === initialRevealed
    ) {
      return;
    }
    this.snap = { hasPending, timedOut, initialRevealed };
    for (const listener of this.listeners) listener();
  }
}

export interface TimelineImageScopeValue {
  readonly initialRevealed: boolean;
  readonly initialGate: ImageGateStore;
}

export const TimelineImageScopeContext = createContext<TimelineImageScopeValue | null>(null);
export const TimelineItemImageBatchContext = createContext<ImageGateStore | null>(null);

function ImageLightbox({
  open,
  onClose,
  src,
  alt,
  triggerRef,
}: {
  open: boolean;
  onClose: () => void;
  src: string;
  alt: string;
  triggerRef: RefObject<HTMLElement | null>;
}) {
  const t = useTranslations("common");
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const trigger = triggerRef.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKeyDown);
    dialogRef.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      if (trigger && (trigger instanceof HTMLButtonElement || trigger.tabIndex >= 0)) {
        trigger.focus();
      }
    };
  }, [open, onClose, triggerRef]);

  if (!open) return null;

  return createPortal(
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={alt || t("openImage")}
      data-testid="image-lightbox"
      tabIndex={-1}
      className={cn("fixed inset-0 z-50 items-center justify-center p-4 outline-none", FLEX)}
    >
      <button
        type="button"
        aria-label={t("closeImage")}
        data-testid="image-lightbox-backdrop"
        onClick={(event) => {
          event.stopPropagation();
          onClose();
        }}
        className="absolute inset-0 cursor-zoom-out bg-black/80"
      />
      <div
        role="presentation"
        className="contents"
        onClick={(event) => event.stopPropagation()}
        onKeyDown={(event) => event.stopPropagation()}
      >
        <img src={src} alt={alt} className="relative max-h-[80vh] max-w-[80vw] rounded object-contain shadow-2xl" />
      </div>
      <button
        type="button"
        aria-label={t("closeImage")}
        data-testid="image-lightbox-close"
        onClick={(event) => {
          event.stopPropagation();
          onClose();
        }}
        className="absolute right-4 top-4 z-10 rounded p-2 text-white/80 hover:bg-white/10 hover:text-white"
      >
        <X className="size-5" />
      </button>
    </div>,
    document.body,
  );
}

// meiva:note
// To user document images open at full size on click: the inline image is a button, and the enlarged copy is portalled to document.body (Escape, the close button, or a click on the backdrop dismisses it). Portalling is required because the document pane scrolls inside a contained box, which would clip a fixed layer.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.md
export function ZoomableImage({
  src,
  alt,
  className,
  buttonClassName,
  overlay,
}: {
  src: string;
  alt: string;
  className?: string;
  buttonClassName?: string;
  overlay?: ReactNode;
}) {
  const t = useTranslations("common");
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  const buttonRef = useRef<HTMLButtonElement>(null);

  return (
    <>
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpen(true)}
        aria-label={alt || t("openImage")}
        data-testid="zoomable-image"
        className={cn(
          "relative block cursor-zoom-in rounded border-0 bg-transparent p-0",
          buttonClassName,
        )}
      >
        <img src={src} alt={alt} loading="lazy" className={cn("max-w-full rounded border border-border object-contain", className)} />
        {overlay}
      </button>
      <ImageLightbox open={open} onClose={close} src={src} alt={alt} triggerRef={buttonRef} />
    </>
  );
}

type MarkdownZoomableImageProps = Omit<ComponentPropsWithoutRef<"img">, "src"> & {
  src?: string;
};

const IDLE_GATE_SNAPSHOT: ImageGateSnapshot = {
  hasPending: false,
  timedOut: false,
  initialRevealed: false,
};
const NOOP_GATE_SUBSCRIBE = () => () => undefined;
const GET_IDLE_GATE_SNAPSHOT = () => IDLE_GATE_SNAPSHOT;

function parseHtmlDimension(value: string | number | undefined): number | null {
  if (typeof value === "number") return Number.isFinite(value) && value > 0 ? value : null;
  if (typeof value === "string") {
    const trimmed = value.trim();
    if (!/^\d+(?:\.\d+)?$/.test(trimmed)) return null;
    const parsed = Number(trimmed);
    return Number.isFinite(parsed) && parsed > 0 ? parsed : null;
  }
  return null;
}

// meiva:note
// Default Markdown images sit directly inside <p> (and optionally <a>), keeping valid inline HTML with no wrapper span or corner button: standalone images show `cursor-zoom-in` and open the portalled document.body lightbox on click, whereas images nested inside a Markdown link (`[a_&]:cursor-pointer`) leave link activation untouched; browser <img> elements fetch authenticated workspace bytes directly. Inside a TimelineImageScope, images reserve known-dimension <span> slots with a centered Spinner or coordinate unknown-dimension loads through the timeline's initial-reveal / per-item batch gate to prevent vertical layout shift.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.md
export function MarkdownZoomableImage({ src, alt, ...props }: MarkdownZoomableImageProps) {
  const timelineScope = useContext(TimelineImageScopeContext);
  if (!src) {
    return <img {...props} src={undefined} alt={alt} />;
  }
  if (timelineScope !== null) {
    return <TimelineMarkdownImg {...props} src={src} alt={alt ?? ""} scope={timelineScope} />;
  }
  return <ZoomableMarkdownImg {...props} src={src} alt={alt ?? ""} />;
}

type TimelineMarkdownMode = "known" | "initial-scope" | "item-batch" | "plain";

interface TimelineImageMountState {
  readonly src: string;
  readonly initialDims: ImageDimensions | null;
  readonly mode: TimelineMarkdownMode;
}

function resolveMountState(
  src: string,
  explicitDims: ImageDimensions | null,
  scope: TimelineImageScopeValue,
  batch: ImageGateStore | null,
): TimelineImageMountState {
  const initialDims = explicitDims ?? getCachedImageDimensions(src);
  const mode: TimelineMarkdownMode =
    initialDims !== null
      ? "known"
      : !scope.initialRevealed
        ? "initial-scope"
        : batch !== null
          ? "item-batch"
          : "plain";
  return { src, initialDims, mode };
}

function TimelineMarkdownImg({
  src,
  alt,
  scope,
  ...props
}: Omit<ComponentPropsWithoutRef<"img">, "src" | "alt"> & {
  src: string;
  alt: string;
  scope: TimelineImageScopeValue;
}) {
  const id = useId();
  const batch = useContext(TimelineItemImageBatchContext);
  const explicitDims = normalizeImageDimensions({
    width: parseHtmlDimension(props.width),
    height: parseHtmlDimension(props.height),
  });
  const [mountState, setMountState] = useState<TimelineImageMountState>(() =>
    resolveMountState(src, explicitDims, scope, batch),
  );
  const [loadedDims, setLoadedDims] = useState<ImageDimensions | null>(null);
  const [ownSettled, setOwnSettled] = useState(false);
  const [failed, setFailed] = useState(false);
  const [revealedOnce, setRevealedOnce] = useState(false);
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const settledForSrcRef = useRef<string | null>(null);

  let activeMount = mountState;
  let activeLoadedDims = loadedDims;
  let activeOwnSettled = ownSettled;
  let activeFailed = failed;
  let activeRevealedOnce = revealedOnce;
  if (mountState.src !== src) {
    activeMount = resolveMountState(src, explicitDims, scope, batch);
    setMountState(activeMount);
    setLoadedDims(null);
    setOwnSettled(false);
    setFailed(false);
    setRevealedOnce(false);
    activeLoadedDims = null;
    activeOwnSettled = false;
    activeFailed = false;
    activeRevealedOnce = false;
  }

  const { initialDims, mode } = activeMount;
  const activeGate: ImageGateStore | null =
    mode === "initial-scope" ? scope.initialGate : mode === "item-batch" ? batch : null;

  useLayoutEffect(() => {
    if (!activeGate || activeOwnSettled || settledForSrcRef.current === src) return;
    return activeGate.register(id);
  }, [activeGate, activeOwnSettled, id, src]);

  const batchSnap = useSyncExternalStore(
    mode === "item-batch" && batch ? batch.subscribe : NOOP_GATE_SUBSCRIBE,
    mode === "item-batch" && batch ? batch.getSnapshot : GET_IDLE_GATE_SNAPSHOT,
    GET_IDLE_GATE_SNAPSHOT,
  );

  const batchReady =
    mode !== "item-batch" ||
    activeRevealedOnce ||
    (!batchSnap.hasPending && activeOwnSettled) ||
    batchSnap.timedOut;
  if (mode === "item-batch" && batchReady && !activeRevealedOnce) {
    setRevealedOnce(true);
  }

  const attachImgRef = useCallback(
    (node: HTMLImageElement | null) => {
      imgRef.current = node;
      if (node && node.complete && node.naturalWidth > 0 && node.naturalHeight > 0) {
        const measured = normalizeImageDimensions({
          width: node.naturalWidth,
          height: node.naturalHeight,
        });
        if (measured) {
          setCachedImageDimensions(src, measured.width, measured.height);
          setLoadedDims((prev) => prev ?? measured);
        }
        settledForSrcRef.current = src;
        setOwnSettled(true);
        activeGate?.settle(id);
      }
    },
    [activeGate, id, src],
  );

  const slotDims = mode === "known" ? initialDims : activeLoadedDims;
  const waitingForBatch = mode === "item-batch" && !batchReady;
  const showKnownSpinner = mode === "known" && !activeOwnSettled && !activeFailed;

  const { onLoad, onError, onClick, className, width: _w, height: _h, loading: _l, ...restProps } =
    props;

  // meiva:note
  // Timeline Markdown images stay inside phrasing <span> containers so they remain valid HTML
  // inside <p> and <a> while reserving dimensions or batch-preloading hidden at 0x0.
  // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
  const img = <img
    {...restProps}
    ref={attachImgRef}
    src={src}
    alt={alt}
    loading="eager"
    width={waitingForBatch ? undefined : (slotDims?.width ?? props.width)}
    height={waitingForBatch ? undefined : (slotDims?.height ?? props.height)}
    onLoad={(event) => {
      onLoad?.(event);
      const el = event.currentTarget;
      const measured = normalizeImageDimensions({
        width: el.naturalWidth,
        height: el.naturalHeight,
      });
      if (measured) {
        setCachedImageDimensions(src, measured.width, measured.height);
        setLoadedDims(measured);
      }
      settledForSrcRef.current = src;
      setOwnSettled(true);
      activeGate?.settle(id);
    }}
    onError={(event) => {
      onError?.(event);
      settledForSrcRef.current = src;
      setFailed(true);
      setOwnSettled(true);
      activeGate?.settle(id);
    }}
    onClick={(event) => {
      onClick?.(event);
      if (waitingForBatch || event.defaultPrevented || event.currentTarget.closest("a")) return;
      setOpen(true);
    }}
    className={
      waitingForBatch
        ? "pointer-events-none absolute size-0 opacity-0"
        : cn(
            "block cursor-zoom-in [a_&]:cursor-pointer",
            slotDims ? "absolute inset-0 h-full w-full object-contain" : "h-auto max-w-full",
            mode === "known" && "transition-opacity duration-150",
            showKnownSpinner ? "opacity-0" : "opacity-100",
            className,
          )
    }
  />;

  return (
    <>
      <span
        data-testid={waitingForBatch ? "timeline-image-batch-pending" : "timeline-image-slot"}
        style={
          !waitingForBatch && slotDims
            ? {
                width: `${slotDims.width}px`,
                aspectRatio: `${slotDims.width} / ${slotDims.height}`,
              }
            : undefined
        }
        className={
          waitingForBatch
            ? "relative inline-flex items-center align-middle"
            : cn(
                "relative my-1 align-middle",
                slotDims
                  ? cn(
                      "inline-block max-w-full rounded border border-border",
                      showKnownSpinner && "bg-muted/30",
                      OVERFLOW_HIDDEN,
                    )
                  : "inline-flex max-w-full",
              )
        }
      >
        {waitingForBatch ? (
          <span
            data-testid="timeline-image-placeholder"
            aria-hidden="true"
            className="inline-flex items-center justify-center"
          >
            <Spinner className="size-3.5" />
          </span>
        ) : showKnownSpinner ? (
          <span
            data-testid="timeline-image-placeholder"
            aria-hidden="true"
            className={cn("pointer-events-none absolute inset-0 items-center justify-center", FLEX)}
          >
            <Spinner className="size-4" />
          </span>
        ) : null}
        {img}
      </span>
      <ImageLightbox open={open} onClose={close} src={src} alt={alt} triggerRef={imgRef} />
    </>
  );
}

function ZoomableMarkdownImg({
  src,
  alt,
  ...props
}: Omit<ComponentPropsWithoutRef<"img">, "src" | "alt"> & { src: string; alt: string }) {
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), [setOpen]);
  const imgRef = useRef<HTMLImageElement>(null);

  // meiva:note
  // Standalone Markdown images remain phrasing <img> elements inside <p> with pointer click-to-zoom only.
  // eslint-disable-next-line jsx-a11y/click-events-have-key-events, jsx-a11y/no-noninteractive-element-interactions
  const img = <img
    {...props}
    ref={imgRef}
    src={src}
    alt={alt}
    className={cn("cursor-zoom-in [a_&]:cursor-pointer", props.className)}
    onClick={(event) => {
      props.onClick?.(event);
      if (event.defaultPrevented || event.currentTarget.closest("a")) return;
      setOpen(true);
    }}
  />;

  return (
    <>
      {img}
      <ImageLightbox open={open} onClose={close} src={src} alt={alt} triggerRef={imgRef} />
    </>
  );
}
