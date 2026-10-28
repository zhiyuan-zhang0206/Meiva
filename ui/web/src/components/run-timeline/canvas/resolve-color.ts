// meiva:note
// The opaque canvas color of a CSS color expression (variables and `color-mix` included), cached until
// the theme changes; both the row canvases and the arrows canvas resolve their colors through it.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/agent-links.meiva.okf.md

import type { Resolve } from "./run-timeline-paint";

let probe: HTMLElement | null = null;
const resolved = new Map<string, string>();
let watching = false;

export const resolveColor: Resolve = (css) => {
  const known = resolved.get(css);
  if (known !== undefined) return known;
  if (typeof document === "undefined") return css;
  if (!watching) {
    watching = true;
    const clear = () => resolved.clear();
    new MutationObserver(clear).observe(document.documentElement, { attributes: true });
    if (typeof window.matchMedia === "function") window.matchMedia("(prefers-color-scheme: dark)").addEventListener("change", clear);
  }
  if (probe === null) {
    probe = document.createElement("span");
    probe.setAttribute("aria-hidden", "true");
    probe.style.display = "none";
    document.body.appendChild(probe);
  }
  probe.style.backgroundColor = "";
  probe.style.backgroundColor = css;
  const color = getComputedStyle(probe).backgroundColor || css;
  resolved.set(css, color);
  return color;
};
