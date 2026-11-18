// meiva:note
// Measure a prefix to a fixpoint before restoring/prepending around its anchor.
// Keep successful measurements materialized: returning to auto can restore
// intrinsic estimates and move the reader after compensation. Bounded mounting
// releases the measured nodes as the conversation scrolls.
export function materializeBeforeAnchor(viewport: HTMLElement, anchor: HTMLElement) {
  const saved: { el: HTMLElement; cv: string }[] = [];
  let anchorTop = anchor.getBoundingClientRect().top;
  for (;;) {
    let forcedAny = false;
    for (const el of viewport.querySelectorAll<HTMLElement>(".timeline-item")) {
      if (el === anchor || el.contains(anchor)) break;
      if (el.style.contentVisibility === "visible") continue;
      if (el.getBoundingClientRect().top >= anchorTop) break;
      saved.push({ el, cv: el.style.contentVisibility });
      el.style.contentVisibility = "visible";
      forcedAny = true;
    }
    if (!forcedAny) break;
    const nextTop = anchor.getBoundingClientRect().top;
    if (nextTop === anchorTop) break;
    anchorTop = nextTop;
  }
  return saved;
}

// meiva:note
// Saved reading positions inside a tall off-screen item (`viewportTop < 0`)
// depend on the anchor's own rendered height as well as any trailing items
// needed to satisfy `scrollHeight - clientHeight >= targetTop` while CSS
// `content-visibility: auto` still holds intrinsic 80px placeholders.
export function materializeFromAnchor(
  viewport: HTMLElement,
  anchor: HTMLElement,
  requiredTop?: number,
): void {
  if (anchor.classList.contains("timeline-item") && anchor.style.contentVisibility !== "visible") {
    anchor.style.contentVisibility = "visible";
  }
  for (const child of anchor.querySelectorAll<HTMLElement>(".timeline-item")) {
    if (child.style.contentVisibility !== "visible") {
      child.style.contentVisibility = "visible";
    }
  }
  if (requiredTop === undefined || viewport.scrollHeight - viewport.clientHeight >= requiredTop) {
    return;
  }
  let seenAnchor = false;
  for (const el of viewport.querySelectorAll<HTMLElement>(".timeline-item")) {
    if (el === anchor || anchor.contains(el)) {
      seenAnchor = true;
      continue;
    }
    if (!seenAnchor || el.style.contentVisibility === "visible") continue;
    el.style.contentVisibility = "visible";
    if (viewport.scrollHeight - viewport.clientHeight >= requiredTop) break;
  }
}
