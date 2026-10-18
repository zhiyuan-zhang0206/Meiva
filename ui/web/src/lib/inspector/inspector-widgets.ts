// meiva:link ui/web/src/docs/frontend-components/inspector/inspector.meiva.okf.md
export const INSPECT_SECTION_ORDER = {
  costActivity: 900,
  context: 600,
  runLink: 100,
  page: 200,
  shells: 300,
  liveness: 400,
  configOverlay: 500,
  notice: 800,
} as const;

export function fleetTaskHref(id: number): `/fleet?task=${number}` {
  return `/fleet?task=${id}`;
}

export function fleetNoticeHref(id: number): `/fleet?notice=${number}` {
  return `/fleet?notice=${id}`;
}
