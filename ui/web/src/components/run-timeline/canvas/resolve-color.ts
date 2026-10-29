// meiva:note
// The opaque canvas color of a CSS color expression (variables and `color-mix` included), cached
// until the theme changes; both the row canvases and the arrows canvas resolve their colors
// through it.
//
// The resolver is a value built from the browser port's media answers (`colorResolver`), so a
// component takes it from `useColorResolver` and the paint code receives it — nothing here asks
// the window which color scheme it prefers.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/agent-links.meiva.okf.md

import { useMemo } from "react";

import type { BrowserMedia } from "@/lib/browser/browser";
import { useBrowser } from "@/lib/browser/browser-provider";

import type { Resolve } from "./run-timeline-paint";

export function colorResolver(media: BrowserMedia): Resolve {
  let probe: HTMLElement | null = null;
  const resolved = new Map<string, string>();
  let watching = false;
  const watch = (): void => {
    if (watching) return;
    watching = true;
    const clear = () => resolved.clear();
    new MutationObserver(clear).observe(document.documentElement, { attributes: true });
    media.query("(prefers-color-scheme: dark)").subscribe(clear);
  };
  return (css) => {
    const known = resolved.get(css);
    if (known !== undefined) return known;
    if (typeof document === "undefined") return css;
    watch();
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
}

// meiva:note
// The resolver for the page this component runs in, rebuilt only when the media answers change.
export function useColorResolver(): Resolve {
  const browser = useBrowser();
  return useMemo(() => colorResolver(browser.media), [browser.media]);
}
