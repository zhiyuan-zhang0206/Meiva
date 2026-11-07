export const FILE_PARAM = "file";

// meiva:note
// The digest parameter panel links carried before 2026-11-07. A shared link outlives the spelling
// of the address bar, so a chain of digests is still read — the owning machine resolves it — and
// the panel's next navigation rewrites the entry with `file`.
// meiva:link ui/web/src/docs/frontend-state/navigation-history.meiva.okf.md
export const LEGACY_PARAM = "reference";

// meiva:note
// A chain element is the file's readable name, so the address bar reads
// `?agent_id=405&file=reports/runbook.md` instead of a digest. Form encoding would write the
// separators as `%2F` and undo that, so a file keeps its `/` and only what a query string
// cannot carry is escaped.
// meiva:link ui/web/src/docs/frontend-state/navigation-history.meiva.okf.md
export function encodeFile(value: string): string {
  return encodeURIComponent(value).replace(/%2F/gi, "/");
}

export function fileQuery(files: readonly string[]): string {
  return files.map((file) => `${FILE_PARAM}=${encodeFile(file)}`).join("&");
}

export function readFiles(url: URL, agentId: number): string[] {
  if (Number(url.searchParams.get("agent_id")) !== agentId) return [];
  const files = url.searchParams.getAll(FILE_PARAM);
  return files.length ? files : url.searchParams.getAll(LEGACY_PARAM);
}

// meiva:note
// The chain is replaced in place so the rest of the address keeps its own spelling, and the
// files stay last: a pushed entry then reads `?agent_id=…&file=…&file=…`.
export function withFiles(href: string, files: readonly string[]): string {
  const url = new URL(href);
  const kept = url.search
    .replace(/^\?/, "")
    .split("&")
    .filter((part) => part !== "" && ![FILE_PARAM, LEGACY_PARAM].includes(part.split("=")[0]));
  url.search = [...kept, ...(files.length ? [fileQuery(files)] : [])].join("&");
  return url.toString();
}
