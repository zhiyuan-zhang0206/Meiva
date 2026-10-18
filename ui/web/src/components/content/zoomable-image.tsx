"use client";

import { X } from "lucide-react";
import { useTranslations } from "next-intl";
import { useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { FLEX } from "@/lib/layout/layout";
import { cn } from "@/lib/format/utils";

// meiva:note
// To user document images open at full size on click: the inline image is a button, and the enlarged copy is portalled to document.body (Escape, the close button, or a click on the backdrop dismisses it). Portalling is required because the document pane scrolls inside a contained box, which would clip a fixed layer.
// meiva:link ui/web/src/docs/frontend-components/markdown-surfaces.meiva.okf.md
export function ZoomableImage({ src, alt, className }: {
  src: string;
  alt: string;
  className?: string;
}) {
  const t = useTranslations("common");
  const [open, setOpen] = useState(false);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const dialogRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const trigger = buttonRef.current;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    window.addEventListener("keydown", onKeyDown);
    dialogRef.current?.focus();
    return () => {
      window.removeEventListener("keydown", onKeyDown);
      document.body.style.overflow = previousOverflow;
      trigger?.focus();
    };
  }, [open]);

  const thumbnail = (
    <img src={src} alt={alt} loading="lazy" className={cn("max-w-full rounded border border-border object-contain", className)} />
  );
  const enlarged = (
    <img src={src} alt={alt} className="relative max-h-[80vh] max-w-[80vw] rounded object-contain shadow-2xl" />
  );

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
        {thumbnail}
      </button>
      {open
        ? createPortal(
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
                onClick={() => setOpen(false)}
                className="absolute inset-0 cursor-zoom-out bg-black/80"
              />
              {enlarged}
              <button
                type="button"
                aria-label={t("closeImage")}
                data-testid="image-lightbox-close"
                onClick={() => setOpen(false)}
                className="absolute right-4 top-4 z-10 rounded p-2 text-white/80 hover:bg-white/10 hover:text-white"
              >
                <X className="size-5" />
              </button>
            </div>,
            document.body,
          )
        : null}
    </>
  );
}
