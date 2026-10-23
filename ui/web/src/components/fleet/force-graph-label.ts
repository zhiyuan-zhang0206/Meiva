export function radiusOf(score: number, maxScore: number, minR: number, maxR: number): number {
  const ratio = maxScore > 0 ? score / maxScore : 0;
  return minR + (maxR - minR) * Math.sqrt(ratio);
}

export const LABEL_MIN_ZOOM = 0.45;
export const LABEL_LINE_HEIGHT = 7;
export const GLYPH_WIDTH_LATIN = 3.6;
export const GLYPH_WIDTH_FULL = 6;

const FULL_WIDTH_RE =
  /[\u2E80-\u303F\u3040-\u30FF\u3400-\u4DBF\u4E00-\u9FFF\uAC00-\uD7AF\uF900-\uFAFF\uFF00-\uFFEF\u2600-\u27BF\u{1F000}-\u{1FAFF}\u2026]/u;

function glyphWidth(ch: string): number {
  return FULL_WIDTH_RE.test(ch) ? GLYPH_WIDTH_FULL : GLYPH_WIDTH_LATIN;
}

function charsThatFit(text: string, maxWidth: number): number {
  let width = 0;
  let i = 0;
  for (const ch of text) {
    const w = glyphWidth(ch);
    if (width + w > maxWidth + 1e-9) break;
    width += w;
    i += ch.length;
  }
  return i;
}

export function wrapLabel(label: string, r: number): string[] {
  const maxLineWidth = 2 * r - 8;
  const maxLabelLines = Math.max(Math.floor((2 * r - 4) / LABEL_LINE_HEIGHT) - 1, 0);
  if (maxLineWidth < GLYPH_WIDTH_LATIN || maxLabelLines === 0 || label.length === 0) return [];

  const lines: string[] = [];
  let remaining = label.trim();
  while (charsThatFit(remaining, maxLineWidth) < remaining.length) {
    const cut = charsThatFit(remaining, maxLineWidth);
    if (cut <= 0) break;

    if (remaining[cut] === " ") {
      lines.push(remaining.slice(0, cut));
      remaining = remaining.slice(cut + 1).trimStart();
      continue;
    }

    const candidate = remaining.slice(0, cut);
    const spaceIndex = candidate.lastIndexOf(" ");
    const hyphenIndex = candidate.lastIndexOf("-");
    const breakIndex = Math.max(spaceIndex, hyphenIndex);
    if (breakIndex > 0) {
      const includeBreak = candidate[breakIndex] === "-";
      lines.push(candidate.slice(0, breakIndex + (includeBreak ? 1 : 0)).trimEnd());
      remaining = remaining.slice(breakIndex + 1).trimStart();
      continue;
    }

    if (cut > 0 && !FULL_WIDTH_RE.test(remaining[cut - 1])) {
      let runStart = cut - 1;
      while (runStart > 0 && !FULL_WIDTH_RE.test(remaining[runStart - 1])) runStart--;
      if (runStart > 0) {
        lines.push(remaining.slice(0, runStart).trimEnd());
        remaining = remaining.slice(runStart).trimStart();
        continue;
      }
    }

    lines.push(remaining.slice(0, cut));
    remaining = remaining.slice(cut);
  }
  if (remaining.length > 0) lines.push(remaining);
  if (lines.length <= maxLabelLines) return lines;

  const visibleLines = lines.slice(0, maxLabelLines);
  const last = visibleLines.length - 1;
  const keep = charsThatFit(visibleLines[last], maxLineWidth - glyphWidth("…"));
  visibleLines[last] = `${visibleLines[last].slice(0, keep)}…`;
  return visibleLines;
}
