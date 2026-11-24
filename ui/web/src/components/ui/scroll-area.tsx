"use client"

import * as React from "react"
import * as ScrollAreaPrimitive from "@radix-ui/react-scroll-area"

import { cn } from "@/lib/format/utils"
import { FLEX, FLEX_1, OVERFLOW_HIDDEN } from "@/lib/layout/layout";
import { useNonce } from "@/lib/auth/nonce-context";

const SCROLLBAR_IDLE_DELAY_MS = 800
const SCROLL_KEYS = new Set(["ArrowUp", "ArrowDown", "PageUp", "PageDown", "Home", "End", " "])

function positionAxisThumb(
  viewport: HTMLElement,
  track: HTMLElement | null,
  thumb: HTMLElement | null,
  orientation: "vertical" | "horizontal",
) {
  if (!track || !thumb) return
  const vertical = orientation === "vertical"
  const maxScroll = vertical
    ? viewport.scrollHeight - viewport.clientHeight
    : viewport.scrollWidth - viewport.clientWidth
  if (maxScroll <= 0) {
    thumb.style.transform = "translate3d(0, 0, 0)"
    return
  }
  const trackStyle = getComputedStyle(track)
  const padStart = Number.parseFloat(vertical ? trackStyle.paddingTop : trackStyle.paddingLeft) || 0
  const padEnd = Number.parseFloat(vertical ? trackStyle.paddingBottom : trackStyle.paddingRight) || 0
  const travel = Math.max(
    0,
    (vertical ? track.clientHeight : track.clientWidth) - padStart - padEnd -
      (vertical ? thumb.offsetHeight : thumb.offsetWidth),
  )
  const offset = ((vertical ? viewport.scrollTop : viewport.scrollLeft) / maxScroll) * travel
  thumb.style.transform = vertical
    ? `translate3d(0, ${offset}px, 0)`
    : `translate3d(${offset}px, 0, 0)`
}

function ScrollArea({
  className,
  viewportClassName,
  scrollbarClassName,
  viewportRef: externalViewportRef,
  viewportTestId,
  viewportId,
  showHorizontalScrollbar = false,
  scrollbarActivity = "scroll",
  children,
  onScrollCapture,
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.Root> & {
  // Extra classes for the inner scroll viewport (the element that actually
  // scrolls). Used e.g. by the timeline to set `overflow-anchor: none`.
  viewportClassName?: string;
  scrollbarClassName?: string;
  viewportRef?: React.RefObject<HTMLDivElement | null>;
  viewportTestId?: string;
  viewportId?: string;
  showHorizontalScrollbar?: boolean;
  scrollbarActivity?: "scroll" | "input";
}) {
  const [isScrollActive, setIsScrollActive] = React.useState(false)
  const [isDraggingScrollbar, setIsDraggingScrollbar] = React.useState(false)
  // Radix injects a <style> hiding the native scrollbar; the production
  // style-src only applies it when it carries the page nonce. Forward the
  // nonce so the injection is allowed instead of console-refused; the rule
  // compiled in globals.css stays as the nonce-less fallback.
  const nonce = useNonce()
  const idleTimerRef = React.useRef<number | null>(null)

  // Element refs for the direct thumb positioning in positionThumb below.
  const viewportRef = React.useRef<HTMLDivElement | null>(null)
  const setViewportRef = React.useCallback((element: HTMLDivElement | null) => {
    viewportRef.current = element
    if (externalViewportRef) externalViewportRef.current = element
  }, [externalViewportRef])
  const trackRef = React.useRef<HTMLDivElement | null>(null)
  const thumbRef = React.useRef<HTMLDivElement | null>(null)
  const horizontalTrackRef = React.useRef<HTMLDivElement | null>(null)
  const horizontalThumbRef = React.useRef<HTMLDivElement | null>(null)

  const clearIdleTimer = React.useCallback(() => {
    if (idleTimerRef.current !== null) {
      window.clearTimeout(idleTimerRef.current)
      idleTimerRef.current = null
    }
  }, [])

  const showScrollbarUntilIdle = React.useCallback(() => {
    clearIdleTimer()
    setIsScrollActive(true)
    idleTimerRef.current = window.setTimeout(() => {
      setIsScrollActive(false)
      idleTimerRef.current = null
    }, SCROLLBAR_IDLE_DELAY_MS)
  }, [clearIdleTimer])

  React.useEffect(() => clearIdleTimer, [clearIdleTimer])

  // Radix positions the thumb via an internal rAF polling loop that is armed
  // only on the FIRST scroll event of a gesture (addUnlinkedScrollListener);
  // when that loop is absent from the bundle the thumb freezes for the whole
  // gesture and only catches up at the next discrete scroll event — the
  // "scrollbar does not follow the content" report (2026-09-08, messages +
  // agent tree). Reposition the thumb directly from the viewport's scroll
  // events here instead: every event (60 Hz during a wheel/trackpad gesture)
  // moves the thumb in real time, with no dependency on Radix internals.
  // When Radix's loop IS present it writes the same value, so the two never
  // conflict.
  const positionThumb = React.useCallback(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    positionAxisThumb(viewport, trackRef.current, thumbRef.current, "vertical")
    positionAxisThumb(viewport, horizontalTrackRef.current, horizontalThumbRef.current, "horizontal")
  }, [])

  // Radix renders the thumb only while content overflows; when it (un)mounts
  // (fit -> overflow transition) position it right away.
  const setThumbRef = React.useCallback(
    (el: HTMLDivElement | null) => {
      thumbRef.current = el
      if (el) positionThumb()
    },
    [positionThumb],
  )
  const setHorizontalThumbRef = React.useCallback(
    (el: HTMLDivElement | null) => {
      horizontalThumbRef.current = el
      if (el) positionThumb()
    },
    [positionThumb],
  )

  React.useEffect(() => {
    const viewport = viewportRef.current
    if (!viewport) return
    viewport.addEventListener("scroll", positionThumb, { passive: true })
    // meiva:note
    // A viewport resize (window resize / panel drag) changes the scroll range
    // and thumb travel; re-anchor even when no scroll event follows.
    let resizeObserver: ResizeObserver | null = null
    if (typeof ResizeObserver !== "undefined") {
      resizeObserver = new ResizeObserver(() => positionThumb())
      resizeObserver.observe(viewport)
      if (viewport.firstElementChild) resizeObserver.observe(viewport.firstElementChild)
    }
    positionThumb()
    return () => {
      viewport.removeEventListener("scroll", positionThumb)
      resizeObserver?.disconnect()
    }
  }, [positionThumb])

  const scrollbarState = {
    "data-visible": isScrollActive || isDraggingScrollbar,
    onPointerDown: (event: React.PointerEvent) => {
      if (event.button !== 0) return
      clearIdleTimer()
      setIsDraggingScrollbar(true)
    },
    onPointerUp: () => {
      if (!isDraggingScrollbar) return
      setIsDraggingScrollbar(false)
      showScrollbarUntilIdle()
    },
    onPointerCancel: () => {
      if (!isDraggingScrollbar) return
      setIsDraggingScrollbar(false)
      showScrollbarUntilIdle()
    },
  }

  return (
    <ScrollAreaPrimitive.Root
      data-slot="scroll-area"
      type="always"
      className={cn("relative", className, OVERFLOW_HIDDEN)}
      {...props}
      onScrollCapture={(event) => {
        onScrollCapture?.(event)
        if (event.target !== event.currentTarget && event.target !== viewportRef.current) return
        if (scrollbarActivity === "scroll" || idleTimerRef.current !== null) showScrollbarUntilIdle()
      }}
    >
      <ScrollAreaPrimitive.Viewport
        ref={setViewportRef}
        nonce={nonce}
        data-slot="scroll-area-viewport"
        id={viewportId}
        data-testid={viewportTestId}
        onWheel={scrollbarActivity === "input" ? (event) => {
          if (!event.ctrlKey && event.deltaY !== 0 && Math.abs(event.deltaY) >= Math.abs(event.deltaX) &&
              event.target instanceof Element && event.target.closest('[data-slot="scroll-area-viewport"]') === event.currentTarget) {
            showScrollbarUntilIdle()
          }
        } : undefined}
        onTouchMove={scrollbarActivity === "input" ? (event) => {
          if (event.target instanceof Element && event.target.closest('[data-slot="scroll-area-viewport"]') === event.currentTarget) {
            showScrollbarUntilIdle()
          }
        } : undefined}
        onKeyDown={scrollbarActivity === "input" ? (event) => {
          const target = event.target
          if (!event.defaultPrevented && SCROLL_KEYS.has(event.key) && target instanceof Element &&
              target.closest('[data-slot="scroll-area-viewport"]') === event.currentTarget &&
              !target.closest('input, textarea, select, [contenteditable]') &&
              !(event.key === " " && target.closest('button, [role="button"]'))) {
            showScrollbarUntilIdle()
          }
        } : undefined}
        className={cn(
          "size-full rounded-[inherit] transition-[color,box-shadow] outline-none focus-visible:ring-[3px] focus-visible:ring-ring/50 focus-visible:outline-1",
          viewportClassName,
        )}
      >
        {children}
      </ScrollAreaPrimitive.Viewport>
      <ScrollBar
        className={scrollbarClassName}
        trackRef={trackRef}
        thumbRef={setThumbRef}
        {...scrollbarState}
      />
      {showHorizontalScrollbar && (
        <ScrollBar
          orientation="horizontal"
          className={scrollbarClassName}
          trackRef={horizontalTrackRef}
          thumbRef={setHorizontalThumbRef}
          {...scrollbarState}
        />
      )}
      <ScrollAreaPrimitive.Corner />
    </ScrollAreaPrimitive.Root>
  )
}

function ScrollBar({
  className,
  orientation = "vertical",
  trackRef,
  thumbRef,
  ...props
}: React.ComponentProps<typeof ScrollAreaPrimitive.ScrollAreaScrollbar> & {
  // Internal plumbing: ScrollArea drives the thumb's translate directly from
  // viewport scroll events (see positionThumb), so it needs the track and
  // thumb elements.
  trackRef?: React.Ref<HTMLDivElement>;
  thumbRef?: React.Ref<HTMLDivElement>;
}) {
  return (
    <ScrollAreaPrimitive.ScrollAreaScrollbar
      ref={trackRef}
      data-slot="scroll-area-scrollbar"
      orientation={orientation}
      className={cn(
        "touch-none p-px opacity-0 transition-opacity duration-300 select-none hover:opacity-100 focus-within:opacity-100 data-[visible=true]:opacity-100 data-[orientation=horizontal]:h-2.5 data-[orientation=horizontal]:flex-col data-[orientation=horizontal]:border-t data-[orientation=horizontal]:border-t-transparent data-[orientation=vertical]:h-full data-[orientation=vertical]:w-2.5 data-[orientation=vertical]:border-l data-[orientation=vertical]:border-l-transparent",
        className,
        FLEX
      )}
      {...props}
    >
      <ScrollAreaPrimitive.ScrollAreaThumb
        ref={thumbRef}
        data-slot="scroll-area-thumb"
        tabIndex={0}
        className={cn("relative rounded-full bg-border", FLEX_1)}
      />
    </ScrollAreaPrimitive.ScrollAreaScrollbar>
  )
}

export { ScrollArea, ScrollBar }
