// meiva:note
// The run-timeline arrows take one CSS token per kind, drawn 1.6 px wide straight over the chart's card,
// so every kind clears 4.5:1 against `--card` in both themes (the fixed hexes fell to 1.4-3.1:1 once
// drawn, task #5712).
// meiva:link ui/web/src/docs/frontend-components/palette.meiva.okf.md

import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

const css = readFileSync(join(__dirname, "globals.css"), "utf8");


function blockOf(selector: string): Map<string, string> {
  const start = css.indexOf(`${selector} {`);
  expect(start, `${selector} must be declared`).toBeGreaterThan(-1);
  const end = css.indexOf("\n}", start);
  const body = css.slice(start, end);
  const tokens = new Map<string, string>();
  for (const match of body.matchAll(/--([\w-]+):\s*([^;]+);/g)) tokens.set(match[1], match[2].trim());
  return tokens;
}


function luminanceOf(color: string): number {
  const hex = /^#([0-9a-f]{6})$/i.exec(color);
  if (hex !== null) {
    const channels = [0, 2, 4].map((at) => parseInt(hex[1].slice(at, at + 2), 16) / 255);
    const [r, g, b] = channels.map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
    return 0.2126 * r + 0.7152 * g + 0.0722 * b;
  }
  const grey = /^oklch\(\s*([\d.]+)\s+0\s+0\s*\)$/.exec(color);
  // meiva:note
  // An achromatic Oklab color: its lightness is the cube root of the luminance.
  if (grey !== null) return Number(grey[1]) ** 3;
  throw new Error(`no luminance for ${color}`);
}

function contrast(a: string, b: string): number {
  const [high, low] = [luminanceOf(a), luminanceOf(b)].sort((x, y) => y - x);
  return (high + 0.05) / (low + 0.05);
}

const KINDS = [
  "message",
  "user",
  "spawn",
  "fork",
  "terminate",
  "restart",
  "resurrect",
  "notice",
];

describe.each([
  ["light", ":root"],
  ["dark", ".dark"],
])("run-timeline arrow palette (%s)", (_name, selector) => {
  const tokens = blockOf(selector);
  const card = tokens.get("card");

  it("declares one token per arrow kind", () => {
    for (const kind of KINDS) expect(tokens.get(`link-${kind}`), `--link-${kind}`).toMatch(/^#[0-9a-f]{6}$/i);
  });

  it("keeps every kind at 4.5:1 or better against the chart's card", () => {
    expect(card).toBeDefined();
    for (const kind of KINDS) {
      const token = tokens.get(`link-${kind}`)!;
      expect(contrast(token, card!), `--link-${kind} (${token}) vs --card (${card})`).toBeGreaterThanOrEqual(4.5);
    }
  });

  it("gives every kind its own color", () => {
    const values = KINDS.map((kind) => tokens.get(`link-${kind}`));
    expect(new Set(values).size).toBe(KINDS.length);
  });
});
