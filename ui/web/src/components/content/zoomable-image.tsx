"use client";

import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import type { ComponentPropsWithoutRef, ReactNode, RefObject } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

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

// meiva:note
// Default Markdown images sit directly inside <p> (and optionally <a>), keeping valid inline HTML with no wrapper span or corner button: standalone images show `cursor-zoom-in` and open the portalled document.body lightbox on click, whereas images nested inside a Markdown link (`[a_&]:cursor-pointer`) leave link activation untouched; browser <img> elements fetch authenticated workspace bytes directly.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.md
export function MarkdownZoomableImage({ src, alt, ...props }: MarkdownZoomableImageProps) {
  if (!src) {
    return <img {...props} src={undefined} alt={alt} />;
  }
  return <ZoomableMarkdownImg {...props} src={src} alt={alt ?? ""} />;
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
