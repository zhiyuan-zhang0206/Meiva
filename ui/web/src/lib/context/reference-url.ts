export const FILE_PARAM = "file";

// meiva:note
// History entries the shared-context panel pushed carry this flag in their state: the panel's Back
// is then the browser's Back, and an entry it did not push is rewritten in place instead.
// meiva:link ui/web/src/docs/frontend-state/navigation-history.md
export const PUSHED_ENTRY = "meivaToUserEntry";

// meiva:note
// A workspace file open in the panel is part of the address too (user ruling 2026-11-20: the
// browser's Back and Forward move between viewed files), after the to-user chain it was opened from:
// `?agent_id=405&file=notes.md&view=/abs/path/report.py&view_agent=405&lines=10-20`.
// meiva:link ui/web/src/docs/frontend-state/navigation-history.md
export const VIEW_PARAM = "view";
export const VIEW_AGENT_PARAM = "view_agent";
export const VIEW_LINES_PARAM = "lines";
export const VIEW_PARAMS: readonly string[] = [VIEW_PARAM, VIEW_AGENT_PARAM, VIEW_LINES_PARAM];

export interface ViewTarget {
  agentId: number;
  path: string;
  lineRange?: string;
}

// meiva:note
// The digest parameter panel links carried before 2026-11-07; an agent switch still strips it.
export const LEGACY_PARAM = "reference";

// meiva:note
// A chain element is the file's readable name, so the address bar reads
// `?agent_id=405&file=reports/runbook.md` instead of a digest. Form encoding would write the
// separators as `%2F` and undo that, so a file keeps its `/` and only what a query string
// cannot carry is escaped.
// meiva:link ui/web/src/docs/frontend-state/navigation-history.md
export function encodeFile(value: string): string {
  return encodeURIComponent(value).replace(/%2F/gi, "/");
}

export function fileQuery(files: readonly string[]): string {
  return files.map((file) => `${FILE_PARAM}=${encodeFile(file)}`).join("&");
}



// meiva:note
// The file the address names: `view`, or for a link from before the panel had tabs the last file of
// its to-user `file` chain (each was a workspace file the document linked to).
export function readView(url: URL): ViewTarget | null {
  const chain = url.searchParams.getAll(FILE_PARAM);
  const path = url.searchParams.get(VIEW_PARAM) ?? chain.at(-1);
  const agentId = Number(url.searchParams.get(VIEW_AGENT_PARAM) ?? url.searchParams.get("agent_id"));
  if (!path || !Number.isInteger(agentId) || agentId <= 0) return null;
  const lineRange = url.searchParams.get(VIEW_LINES_PARAM);
  return { agentId, path, ...(lineRange ? { lineRange } : {}) };
}

// meiva:note
// The view is replaced in place and written last; an old to-user `file` chain is dropped with it.
export function withView(href: string, target: ViewTarget | null): string {
  const url = new URL(href);
  const kept = url.search
    .replace(/^\?/, "")
    .split("&")
    .filter((part) => part !== "" && ![...VIEW_PARAMS, FILE_PARAM, LEGACY_PARAM].includes(part.split("=")[0]));
  const view = target === null ? [] : [
    `${VIEW_PARAM}=${encodeFile(target.path)}`,
    `${VIEW_AGENT_PARAM}=${target.agentId}`,
    ...(target.lineRange ? [`${VIEW_LINES_PARAM}=${encodeFile(target.lineRange)}`] : []),
  ];
  url.search = [...kept, ...view].join("&");
  return url.toString();
}
