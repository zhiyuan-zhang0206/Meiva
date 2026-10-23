import type { RunTimelineLink, RunTimelineResponse, RunTimelineUnit } from "@/lib/contracts/types";

import { identityKey } from "../object-identity";

// meiva:note
// What an arrow is: an event of the record, or (`user_message`) a chat message of the user, which has no event and is told apart from the messages between agents.
export type LinkKind = RunTimelineLink["kind"] | "user_message";

// meiva:note
// The kinds in legend order; a kind is told apart by its color alone.
export const LINK_KINDS: readonly LinkKind[] = ["send_message", "user_message", "spawn", "fork", "terminate", "restart", "resurrect", "notice"];

export const LINK_COLORS: Record<LinkKind, string> = {
  send_message: "#3b82f6",
  user_message: "#92400e",
  spawn: "#22c55e",
  fork: "#14b8a6",
  terminate: "#ef4444",
  restart: "#f59e0b",
  resurrect: "#a855f7",
  notice: "#ec4899",
};

// meiva:note
// The rows an arrow can end in: an agent's Messages row, or the User or Other agents group (`agent` is then the peer, 0 for the user).
export type LinkRow = "units" | "user" | "other";

export interface LinkEnd {
  row: LinkRow;
  agent: number;
  // meiva:note
  // Epoch milliseconds: where on the shared axis.
  ms: number;
}

export interface ResolvedLink {
  // meiva:note
  // Stable across redraws: kind, time, ends and position in the response.
  key: string;
  // meiva:note
  // What it is for the legend, colors and merging; `link.kind` is the record's (a user's message has no record).
  kind: LinkKind;
  link: RunTimelineLink;
  from: LinkEnd;
  to: LinkEnd;
  // meiva:note
  // The agent at the end that is not in the view, when one is (the user is no agent).
  external: number | null;
  // meiva:note
  // The receiver's block the event ends on, when it ends on one: its message is what the details show, as for a selected block.
  block: RunTimelineUnit | null;
  // meiva:note
  // The user's source (`user`, `ui:page:...`), when the user is an end.
  userSource: string | null;
  // meiva:note
  // The event names an inbound row of the receiver in view but no block carries it: the arrow ends at the event's time, not on a block.
  unmatched: boolean;
}

const middle = (unit: Pick<RunTimelineUnit, "start" | "end">) => (Date.parse(unit.start) + Date.parse(unit.end)) / 2;

// meiva:note
// Whether an inbound source is a person (the user's own message), not an agent, a watcher, a shell or the system.
export const isHumanSource = (source: string | null) => source !== null && (source === "user" || source.startsWith("ui:page:"));

const inboundIndex = new WeakMap<RunTimelineResponse, Map<number, RunTimelineUnit>>();

// meiva:note
// A link names its receiving block by `inbound_id`, and finding that block by scanning the agent's units made resolution O(links x units): 3000 links over 52 000 loaded units took ~250 ms, once per viewport sample, which is what froze panning on a deep zoom-out (user report 2026-10-19). The index is built once per response and reused; responses are immutable per window.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/agent-links.meiva.okf.md
function inboundOf(data: RunTimelineResponse): Map<number, RunTimelineUnit> {
  const cached = inboundIndex.get(data);
  if (cached !== undefined) return cached;
  const index = new Map<number, RunTimelineUnit>();
  for (const unit of data.units) if (unit.inbound_id !== null && !index.has(unit.inbound_id)) index.set(unit.inbound_id, unit);
  inboundIndex.set(data, index);
  return index;
}

const RESOLVED_CACHE_MAX = 16;
const resolvedCache = new WeakMap<readonly RunTimelineLink[], Map<string, ResolvedLink[]>>();

// meiva:note
// Resolution is a pure function of the fetched links and the loaded responses, but the page recomputes it on every viewport sample because the arrays around it are rebuilt. The result is cached per links array and response identity, so a pan inside loaded coverage reuses it; fetches and responses are immutable, which is what makes identity exact.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/agent-links.meiva.okf.md
function resolutionKey(inView: ReadonlySet<number>, loaded: ReadonlyMap<number, RunTimelineResponse>): string {
  return `${[...inView].sort((a, b) => a - b).join(",")}|${[...loaded.keys()].sort((a, b) => a - b).join(",")}|${identityKey([...loaded.values()])}`;
}

export function resolveLinks(
  links: readonly RunTimelineLink[],
  inView: ReadonlySet<number>,
  loaded: ReadonlyMap<number, RunTimelineResponse>,
): ResolvedLink[] {
  const key = resolutionKey(inView, loaded);
  let byKey = resolvedCache.get(links);
  if (byKey === undefined) {
    byKey = new Map();
    resolvedCache.set(links, byKey);
  }
  const hit = byKey.get(key);
  if (hit !== undefined) return hit;
  const resolved = resolveLinksUncached(links, inView, loaded);
  if (byKey.size >= RESOLVED_CACHE_MAX) byKey.clear();
  byKey.set(key, resolved);
  return resolved;
}

function resolveLinksUncached(
  links: readonly RunTimelineLink[],
  inView: ReadonlySet<number>,
  loaded: ReadonlyMap<number, RunTimelineResponse>,
): ResolvedLink[] {
  const out: ResolvedLink[] = [];
  links.forEach((link, index) => {
    const ms = Date.parse(link.ts);
    const { sender, receiver } = link;
    const senderIn = sender !== null && inView.has(sender);
    const receiverIn = receiver !== null && inView.has(receiver);
    if (!senderIn && !receiverIn) return;
    if ((senderIn && !loaded.has(sender)) || (receiverIn && !loaded.has(receiver))) return;
    const userEnd = sender === null || receiver === null;
    const outer = (agent: number | null): LinkEnd => (agent === null ? { row: "user", agent: 0, ms } : { row: "other", agent, ms });
    const from: LinkEnd = senderIn ? { row: "units", agent: sender, ms } : outer(sender);
    let to: LinkEnd = outer(receiver);
    let unmatched = false;
    let block: RunTimelineUnit | null = null;
    if (receiverIn) {
      const receiverData = loaded.get(receiver);
      block =
        link.inbound_id === null || receiverData === undefined
          ? null
          : (inboundOf(receiverData).get(link.inbound_id) ?? null);
      unmatched = link.inbound_id !== null && block === null;
      to = { row: "units", agent: receiver, ms: block === null ? ms : middle(block) };
    }
    out.push({
      kind: link.kind,
      key: `${link.kind}-${link.ts}-${link.sender ?? "user"}-${link.receiver ?? "user"}-${index}`,
      link,
      from,
      to,
      external: userEnd || (senderIn && receiverIn) ? null : senderIn ? receiver : sender,
      block,
      userSource: userEnd ? "user" : null,
      unmatched,
    });
  });
  for (const [id, data] of loaded) {
    if (!inView.has(id)) continue;
    for (const unit of data.units) {
      if (unit.kind !== "inbound" || !isHumanSource(unit.source)) continue;
      const ms = Date.parse(unit.start);
      out.push({
        key: `user-${id}-${unit.i0}`,
        kind: "user_message",
        link: { kind: "send_message", ts: unit.start, sender: null, receiver: id, inbound_id: unit.inbound_id, fork_from: null, notice_id: null },
        from: { row: "user", agent: 0, ms },
        to: { row: "units", agent: id, ms: middle(unit) },
        external: null,
        block: unit,
        userSource: unit.source,
        unmatched: false,
      });
    }
  }
  return out;
}

// meiva:note
// The end of a link that stands in `row`, when one does.
export const endIn = (l: ResolvedLink, row: LinkRow): LinkEnd | undefined => (l.from.row === row ? l.from : l.to.row === row ? l.to : undefined);

// meiva:note
// The links of the User group and of the Other agents group, each left to right.
export function groupLinks(links: readonly ResolvedLink[]): { user: ResolvedLink[]; other: ResolvedLink[] } {
  const side = (row: LinkRow) =>
    links.filter((l) => endIn(l, row) !== undefined).sort((a, b) => (endIn(a, row)?.ms ?? 0) - (endIn(b, row)?.ms ?? 0));
  return { user: side("user"), other: side("other") };
}
