import type { LinkEnd, ResolvedLink } from "./resolve";

// meiva:note
// The most arrows the panel shows at once; the merge distance adapts to the viewport to stay within it.
export const MAX_ARROWS = 20;
// meiva:note
// The largest limit that can be asked for: beyond it the arrows are no longer readable anyway.
export const MAX_ARROWS_CAP = 500;

// meiva:note
// The limit a typed text names: a whole number from 1 to `MAX_ARROWS_CAP`; null for anything else (empty, zero, negative, a fraction, a sign, letters, too large).
export function parseArrowLimit(text: string): number | null {
  if (!/^[1-9][0-9]*$/.test(text)) return null;
  const n = Number(text);
  return n <= MAX_ARROWS_CAP ? n : null;
}

// meiva:note
// What clustering needs of an arrow on screen: its identity, which arrows it may merge with, and where its ends are.
export interface Arrow {
  key: string;
  // meiva:note
  // Arrows merge only within a bucket: the same kind between the same two rows (of the same agents).
  bucket: string;
  x0: number;
  x1: number;
}

// meiva:note
// One drawn arrow: a single link, or several merged.
export interface Cluster {
  // meiva:note
  // The link's own key for one link; `group:<first key>:<count>` for several.
  key: string;
  bucket: string;
  // meiva:note
  // The members' keys, left to right by the start.
  members: string[];
  // meiva:note
  // The middle of the members' ends.
  x0: number;
  x1: number;
}

const rowId = (end: LinkEnd) => (end.row === "units" ? `units:${end.agent}` : end.row);

// meiva:note
// The bucket of a link: its kind and the two rows its ends stand in. Arrows merge only within one.
export const bucketOf = (l: ResolvedLink): string => `${l.kind}|${rowId(l.from)}|${rowId(l.to)}`;

// meiva:note
// Arrows grouped by bucket and sorted by their start, ready to be merged at any distance.
export type PreparedArrows = readonly { bucket: string; list: readonly Arrow[] }[];

function prepareArrows(arrows: readonly Arrow[]): PreparedArrows {
  const buckets = new Map<string, Arrow[]>();
  for (const a of arrows) {
    const list = buckets.get(a.bucket);
    if (list === undefined) buckets.set(a.bucket, [a]);
    else list.push(a);
  }
  return [...buckets].map(([bucket, list]) => ({ bucket, list: list.sort((p, q) => p.x0 - q.x0) }));
}

interface Open {
  first: Arrow;
  members: Arrow[];
}

function mergeAt(prepared: PreparedArrows, px: number, emit: ((c: Cluster) => void) | null): number {
  let count = 0;
  for (const { bucket, list } of prepared) {
    const close = (group: Open) => {
      count += 1;
      if (emit === null) return;
      const n = group.members.length;
      const keys = group.members.map((m) => m.key);
      emit({
        key: n === 1 ? keys[0] : `group:${keys[0]}:${n}`,
        bucket,
        members: keys,
        x0: group.members.reduce((sum, m) => sum + m.x0, 0) / n,
        x1: group.members.reduce((sum, m) => sum + m.x1, 0) / n,
      });
    };
    let open: Open[] = [];
    for (const a of list) {
      const stillOpen: Open[] = [];
      let joined = false;
      for (const group of open) {
        if (a.x0 - group.first.x0 >= px) {
          close(group);
          continue;
        }
        if (!joined && Math.abs(a.x1 - group.first.x1) < px) {
          group.members.push(a);
          joined = true;
        }
        stillOpen.push(group);
      }
      if (!joined) stillOpen.push({ first: a, members: [a] });
      open = stillOpen;
    }
    open.forEach(close);
  }
  return count;
}

// meiva:note
// The merge result at a fixed distance.
export function clusterArrows(arrows: readonly Arrow[], px: number): Cluster[] {
  const out: Cluster[] = [];
  mergeAt(prepareArrows(arrows), px, (c) => out.push(c));
  return out;
}

const SEARCH_PRECISION_PX = 0.5;

// meiva:note
// What `clusterToMax` found: the clusters, the distance that gave them, and whether the limit could be kept.
export interface Clustered {
  clusters: Cluster[];
  px: number;
  // meiva:note
  // False when even merging everything the buckets allow leaves more than `max` arrows (arrows of different kinds or rows are never merged).
  withinMax: boolean;
}

export function clusterToMax(arrows: readonly Arrow[], max: number, widest: number): Clustered {
  const prepared = prepareArrows(arrows);
  const out: Cluster[] = [];
  const done = (px: number, withinMax: boolean): Clustered => {
    out.length = 0;
    mergeAt(prepared, px, (c) => out.push(c));
    return { clusters: [...out], px, withinMax };
  };
  if (mergeAt(prepared, 0, null) <= max) return done(0, true);
  if (mergeAt(prepared, widest, null) > max) return done(widest, false);
  let lo = 0;
  let hi = widest;
  while (hi - lo > SEARCH_PRECISION_PX) {
    const mid = (lo + hi) / 2;
    if (mergeAt(prepared, mid, null) <= max) hi = mid;
    else lo = mid;
  }
  return done(hi, true);
}
