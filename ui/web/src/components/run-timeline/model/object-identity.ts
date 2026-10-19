// meiva:note
// Response objects from the query cache are immutable, so their identity — not their fields — is what makes a cache of a pure function over them exact: two payloads that agree on snapshot and window but differ in content still key apart.
// meiva:link ui/web/src/docs/frontend-components/run-timeline/window-reads.meiva.okf.md
const ids = new WeakMap<object, number>();
let nextId = 1;

export function identityOf(value: object): number {
  const known = ids.get(value);
  if (known !== undefined) return known;
  const assigned = nextId;
  nextId += 1;
  ids.set(value, assigned);
  return assigned;
}

export function identityKey(values: readonly (object | undefined | null)[]): string {
  return values.map((value) => (value === undefined || value === null ? "-" : String(identityOf(value)))).join(",");
}
