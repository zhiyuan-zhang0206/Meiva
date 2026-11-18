// meiva:note
// Measure the target and its prefix to a fixpoint before restoring/prepending.
// Keep successful measurements materialized: returning to auto can restore
// intrinsic estimates and move the reader after compensation. Bounded mounting
// releases the measured nodes as the conversation scrolls.
export function materializeBeforeAnchor(viewport: HTMLElement, anchor: HTMLElement) {
  const saved: { el: HTMLElement; cv: string }[] = [];
  // meiva:note
  // A long final row can still use its intrinsic estimate while outside the
  // viewport. Measure that target too, so its real height supplies the range
  // needed to restore a negative offset before scrolling it into view.
  if (anchor.style.contentVisibility !== "visible") {
    saved.push({ el: anchor, cv: anchor.style.contentVisibility });
    anchor.style.contentVisibility = "visible";
  }
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
