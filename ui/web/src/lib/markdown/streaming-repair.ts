// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md
const FENCE_RE = /^ {0,3}(`{3,}|~{3,})/;

type Marker = "**" | "*" | "__" | "_" | "~~";
interface Open {
  marker: Marker;
  at: number;
}

function isWhitespace(ch: string | undefined): boolean {
  return ch === undefined || /\s/.test(ch);
}
function isWordChar(ch: string | undefined): boolean {
  return ch !== undefined && /[\p{L}\p{N}]/u.test(ch);
}

export function repairStreamingMarkdown(prefix: string): string {
  if (prefix.length === 0) return prefix;
  let fence: string | null = null;
  let paragraphStart = 0;
  let lineStart = 0;
  while (lineStart <= prefix.length) {
    let lineEnd = prefix.indexOf("\n", lineStart);
    if (lineEnd === -1) lineEnd = prefix.length;
    const line = prefix.slice(lineStart, lineEnd);
    const m = FENCE_RE.exec(line);
    if (fence === null) {
      if (m) fence = m[1];
      else if (line.trim() === "") paragraphStart = Math.min(lineEnd + 1, prefix.length);
    } else if (m?.[1].startsWith(fence[0]) && m[1].length >= fence.length && line.trim() === m[1]) {
      fence = null;
      paragraphStart = Math.min(lineEnd + 1, prefix.length);
    }
    if (lineEnd >= prefix.length) break;
    lineStart = lineEnd + 1;
  }
  if (fence !== null) return prefix;
  const head = prefix.slice(0, paragraphStart);
  const body = repairParagraph(prefix.slice(paragraphStart));
  return head + body;
}

function repairParagraph(para: string): string {
  let text = para;
  text = text.replace(/!\[[^\]\n]*\]\([^)\n]*$/, "").replace(/!\[[^\]\n]*$/, "");
  text = text.replace(/\[([^[\]\n]*)\]\([^)\n]*$/, "$1");
  text = text.replace(/(^|\n) {0,3}`{1,2}$/, "$1");

  const open: Open[] = [];
  let unusedRunAt = -1;
  let i = 0;
  while (i < text.length) {
    const ch = text[i];
    if (ch === "\\") {
      i += 2;
      continue;
    }
    if (ch === "`") {
      let j = i;
      while (text[j] === "`") j++;
      const run = text.slice(i, j);
      const close = text.indexOf(run, j);
      if (close === -1) {
        if (text.slice(j).trim() === "") return text.slice(0, i) + closers(open);
        return text + run + closers(open);
      }
      i = close + run.length;
      continue;
    }
    if (ch === "*" || ch === "_" || ch === "~") {
      let j = i;
      while (text[j] === ch) j++;
      const len = j - i;
      const prev = text[i - 1];
      const next = text[j];
      let markers: Marker[] = [];
      if (ch === "~") markers = len === 2 ? ["~~"] : [];
      else if (len === 1) markers = [ch];
      else if (len === 2) markers = [ch === "*" ? "**" : "__"];
      else if (len === 3) markers = ch === "*" ? ["**", "*"] : ["__", "_"];
      const canOpen = !isWhitespace(next) && (ch !== "_" || !isWordChar(prev));
      const canClose = !isWhitespace(prev) && (ch !== "_" || !isWordChar(next));
      let used = false;
      for (const marker of markers) {
        const idx = open.map((o) => o.marker).lastIndexOf(marker);
        if (canClose && idx !== -1) {
          open.length = idx;
          used = true;
        } else if (canOpen) {
          open.push({ marker, at: i });
          used = true;
        }
      }
      if (!used && j === text.length) unusedRunAt = i;
      i = j;
      continue;
    }
    i++;
  }
  if (unusedRunAt !== -1) return text.slice(0, unusedRunAt);
  if (open.length === 0) return text;
  const last = open[open.length - 1];
  const after = text.slice(last.at + last.marker.length);
  if (after.trim() === "") return text.slice(0, last.at) + closers(open.slice(0, -1));
  return text + closers(open);
}

function closers(open: Open[]): string {
  return open
    .map((o) => o.marker)
    .reverse()
    .join("");
}
