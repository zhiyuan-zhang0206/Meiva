"use client";

import { Maximize2, X } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ComponentPropsWithoutRef, RefObject } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/format/utils";
import { FLEX } from "@/lib/layout/layout";

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
  triggerRef: RefObject<HTMLButtonElement | null>;
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
      trigger?.focus();
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
export function ZoomableImage({ src, alt, className }: {
  src: string;
  alt: string;
  className?: string;
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
        className="block cursor-zoom-in rounded border-0 bg-transparent p-0"
      >
        <img src={src} alt={alt} loading="lazy" className={cn("max-w-full rounded border border-border object-contain", className)} />
      </button>
      <ImageLightbox open={open} onClose={close} src={src} alt={alt} triggerRef={buttonRef} />
    </>
  );
}

type MarkdownZoomableImageProps = Omit<ComponentPropsWithoutRef<"img">, "src"> & {
  src?: string;
};

// meiva:note
// Default Markdown images sit inside <p> (and optionally <a>), so the wrapper stays a phrasing <span> and only the corner button opens the portalled lightbox while stopping link activation; browser <img> elements fetch authenticated workspace bytes directly.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.md
export function MarkdownZoomableImage({ src, alt, ...props }: MarkdownZoomableImageProps) {
  const t = useTranslations("common");
  const [open, setOpen] = useState(false);
  const close = useCallback(() => setOpen(false), []);
  const buttonRef = useRef<HTMLButtonElement>(null);

  if (!src) {
    return <img {...props} src={undefined} alt={alt} />;
  }

  const resolvedAlt = alt ?? "";
  return (
    <>
      <span className="group relative inline-block max-w-full align-middle">
        <img {...props} src={src} alt={resolvedAlt} />
        <Button
          variant="ghost"
          size="icon-sm"
          aria-label={t("openImage")}
          data-testid="markdown-image-zoom"
          onClick={(event) => {
            event.preventDefault();
            event.stopPropagation();
            buttonRef.current = event.currentTarget;
            setOpen(true);
          }}
          className={cn(
            "absolute right-1.5 top-1.5 z-10",
            "bg-muted/80 hover:bg-muted text-muted-foreground hover:text-foreground",
            "opacity-0 pointer-coarse:opacity-80 group-hover:opacity-100 transition-opacity",
            "focus-visible:opacity-100",
            "border border-border/50",
          )}
        >
          <Maximize2 className="size-3.5" />
        </Button>
      </span>
      <ImageLightbox open={open} onClose={close} src={src} alt={resolvedAlt} triggerRef={buttonRef} />
    </>
  );
}
