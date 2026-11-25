// meiva:link ui/web/src/docs/frontend-components/timeline-view/stream-reveal.md
import {
  REVEAL_MAX_FRAME_DT_MS,
  REVEAL_MAX_LAG_MS,
  REVEAL_MIN_GRAPHEMES_PER_SEC,
  REVEAL_SETTLE_MS,
  REVEAL_SOFT_CAP_GRAPHEMES_PER_SEC,
  REVEAL_TAU_MS,
} from "./constants";

let segmenter: Intl.Segmenter | null = null;
function graphemeSegmenter(): Intl.Segmenter {
  segmenter ??= new Intl.Segmenter(undefined, { granularity: "grapheme" });
  return segmenter;
}

export function smoothRevealSpeed(backlog: number): number {
  const wanted = (backlog * 1000) / REVEAL_TAU_MS;
  return Math.min(REVEAL_SOFT_CAP_GRAPHEMES_PER_SEC, Math.max(REVEAL_MIN_GRAPHEMES_PER_SEC, wanted));
}

interface Deadline {
  cum: number;
  dueMs: number;
}

function commonPrefixLength(a: string, b: string): number {
  const n = Math.min(a.length, b.length);
  let i = 0;
  while (i < n && a.charCodeAt(i) === b.charCodeAt(i)) i++;
  return i;
}

function isHighSurrogate(code: number): boolean {
  return code >= 0xd800 && code <= 0xdbff;
}

export class RevealController {
  private text: string;
  private bounds: number[] = [];
  private pos: number;
  private live: boolean;
  private lastMs: number | null = null;
  private deadlines: Deadline[] = [];

  constructor(initialText: string, live = true) {
    this.text = initialText;
    this.live = live;
    this.appendBounds();
    this.pos = this.bounds.length;
  }

  update(text: string, live: boolean, nowMs: number): void {
    if (text !== this.text && !live && !this.live) {
      this.text = text;
      this.bounds = [];
      this.appendBounds();
      this.pos = this.bounds.length;
      this.deadlines = [];
      return;
    }
    if (text !== this.text) {
      if (text.startsWith(this.text)) {
        this.bounds.pop();
        this.text = text;
        this.appendBounds();
        this.deadlines.push({ cum: this.bounds.length, dueMs: nowMs + REVEAL_MAX_LAG_MS });
      } else {
        const keep = Math.min(commonPrefixLength(this.text, text), this.shownEnd());
        this.text = text;
        this.bounds = [];
        this.appendBounds();
        let kept = 0;
        while (kept < this.bounds.length && this.bounds[kept] <= keep) kept++;
        this.pos = Math.min(this.pos, kept);
        this.deadlines = [{ cum: this.bounds.length, dueMs: nowMs + REVEAL_MAX_LAG_MS }];
      }
    }
    if (this.live && !live) {
      this.deadlines.push({ cum: this.bounds.length, dueMs: nowMs + REVEAL_SETTLE_MS });
    }
    this.live = live;
  }

  resume(nowMs: number): void {
    this.lastMs = nowMs;
  }

  advance(nowMs: number): void {
    const last = this.lastMs ?? nowMs;
    this.lastMs = nowMs;
    const dtMs = Math.min(Math.max(nowMs - last, 0), REVEAL_MAX_FRAME_DT_MS);
    const avail = this.availableGraphemes();
    const backlog = avail - this.pos;
    if (backlog <= 0) {
      this.pos = Math.min(this.pos, avail);
      return;
    }
    let speed = smoothRevealSpeed(backlog);
    this.deadlines = this.deadlines.filter((d) => d.cum > this.pos);
    for (const d of this.deadlines) {
      const remainingMs = Math.max(d.dueMs - nowMs, 1);
      speed = Math.max(speed, ((Math.min(d.cum, avail) - this.pos) * 1000) / remainingMs);
    }
    this.pos = Math.min(avail, this.pos + (speed * dtMs) / 1000);
  }

  snap(): void {
    this.pos = this.availableGraphemes();
  }

  get done(): boolean {
    return this.pos >= this.availableGraphemes();
  }

  shownCount(): number {
    return Math.min(Math.floor(this.pos), this.bounds.length);
  }

  shownEnd(): number {
    const whole = this.shownCount();
    return whole <= 0 ? 0 : this.bounds[whole - 1];
  }

  shown(): string {
    const end = this.shownEnd();
    return end >= this.text.length ? this.text : this.text.slice(0, end);
  }

  backlog(): number {
    return Math.max(0, this.availableGraphemes() - this.pos);
  }

  private availableGraphemes(): number {
    const n = this.bounds.length;
    if (this.live && n > 0 && isHighSurrogate(this.text.charCodeAt(this.text.length - 1))) return n - 1;
    return n;
  }

  private appendBounds(): void {
    const from = this.bounds.length === 0 ? 0 : this.bounds[this.bounds.length - 1];
    for (const seg of graphemeSegmenter().segment(this.text.slice(from))) {
      this.bounds.push(from + seg.index + seg.segment.length);
    }
  }
}
