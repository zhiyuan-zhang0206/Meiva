---
type: doc
title: Mobile, tablet and coarse-pointer UX
description: Orthogonal viewport-geometry and input/viewport-physics contracts across phones, iPads (768–1366px), and iOS/iPadOS WebKit (Safari and CriOS WKWebView).
tags:
- frontend
---

# Mobile, tablet and coarse-pointer UX

Responsive layout and touch ergonomics in `ui/web` separate along two orthogonal axes so tablet widths (`768–1366px`) never mistake spatial room for a mouse and hardware keyboard:

1. **Spatial geometry (`useBreakpoint`)**: viewport width (`< 768px` narrow, `768–1023px` medium, `>= 1024px` large, `>= 1280px` wide) governs column count and split panes only.
2. **Input and viewport physics (`useCoarsePointer`, `@media (pointer: coarse)`, `VisualViewportHeightSync`, `viewport-fit=cover`)**: governs font-size zoom guards, autofill/autocorrect hygiene, touch hit zones, hover/double-click fallbacks, and visual-viewport height pinning.

## Visual viewport and safe-area contract

- `app/layout.tsx` exports `viewportFit: "cover"` and `interactiveWidget: "resizes-content"`, and pads `<body>` with `env(safe-area-inset-top/left/right)`.
- When an iOS/iPadOS `WKWebView` on-screen keyboard opens, `VisualViewportHeightSync` (`components/shell/visual-viewport-height-sync.tsx`) pins `document.documentElement.style.height`, publishes `--visual-viewport-height: <px>` and `--safe-area-bottom: 0px` on `<html>`, and resets root scroll to `(0, 0)` (removing all three inline properties when unpinned or unmounted).
- Fixed full-screen sheets, drawers, command overlays, and centered dialogs (`MobileSidebar`, narrow `SharedContextPanel`, `SearchOverlay`, `DialogContent`, `SlashAutocomplete`, `Composer` textarea max-height, and `QUESTION_REGION_MAX_HEIGHT_CLASS`) size against `var(--visual-viewport-height, 100dvh)` rather than `inset-0` / `100dvh`, and bottom chrome (`[data-testid="composer-band"]`, `[data-testid="sidebar-footer"]`, mobile `/fleet` tab bar, mobile `SharedContextPanel`) pads against `var(--safe-area-bottom, env(safe-area-inset-bottom, 0px))`.

## Input zoom guard and autofill / autocorrect hygiene

- **No focus auto-zoom on WebKit**: `Input` and `Textarea` use `md:pointer-fine:text-sm` and append `pointer-coarse:text-base md:pointer-coarse:text-base` after caller `className` overrides, backed by an unlayered `@media (pointer: coarse)` `font-size: max(16px, 1rem)` rule on editable `<input>`, `<textarea>`, and `<select>` controls so coarse pointers never drop below `16px` at any viewport width.
- **Non-credential autofill suppression**: `Input`, `Textarea`, and raw non-credential fields default to `autoComplete="off"`, `data-form-type="other"`, `data-1p-ignore=""`, and `data-lpignore="true"` so iPadOS/iOS Chrome and Safari do not surface Password / Credit-Card / Address bars on prompts, filters, or config rows. Explicit credential tokens (`username`, `current-password`, `new-password`, `one-time-code`) on `/login` omit those ignore attributes so password managers continue to fill credentials.
- **Technical text fidelity**: Python schedule script editors, config editors, search/filter inputs, and inline agent rename disable smart punctuation and autocorrect (`autoCorrect="off"`, `autoCapitalize="none"`, `spellCheck={false}`); numeric fields declare `inputMode="numeric"` or `"decimal"`.

## Coarse-pointer affordances

- Custom `ScrollArea` overlay scrollbars set `pointer-events: none` under `@media (pointer: coarse)` so right-edge thumb drags scroll content instead of grabbing an invisible rail.
- `ResizableHandle` widens its invisible hit strip to `24px` (`pointer-coarse:before:w-6` / `aria-[orientation=horizontal]:pointer-coarse:before:h-6`) while keeping its `1px` visual hairline.
- Code-block `CopyButton` stays visible on touch (`pointer-coarse:opacity-80`), `AgentHoverCard` suppresses synthetic touch hover, `AgentRow` exposes `Rename` in its long-press `ContextMenu` (with `select-none [-webkit-touch-callout:none]`), `SpawnPopover` uses `layout="stack"` on coarse-pointer tablets, `ForceGraph` and `TaskKanban` support tap-to-select + tap-selected-to-open, and `useTimelineRowInteractions` guards pan state by `pointerId`.
